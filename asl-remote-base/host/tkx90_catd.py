#!/usr/bin/env python3
"""tkx90-catd -- CAT control daemon for a Kenwood TK-690/790/890 remote base.

The TK-x90 has no CAT port. This daemon adds one. It uses the radio's
documented accessory-port features: the DB-25 "CH Select A-D" inputs pick
one of 15 programmed Remote Channels, and the status outputs report
squelch, TX and RSSI. A Raspberry Pi Pico running
../firmware/main.py drives those pins.

Two front ends turn requests from other programs into channel selections:

  * app_rpt (AllStarLink 3) -- a pseudo-terminal that answers the
    Kenwood command set app_rpt uses with ``remote = kenwood``
    (VMC / VW / RBN / PC).  Point ``ioport`` in rpt.conf at it.

  * Hamlib -- a TCP server that speaks the rigctld network protocol, so
    ``rigctl -m 2 -r localhost:4532`` (and any Hamlib "NET rigctl" client)
    can tune the radio.

Every request is matched against the channel plan in tkx90.toml.  A
frequency that is not in the plan is rejected.  app_rpt then announces
"invalid frequency" and Hamlib clients get RPRT -1.  The radio can only
transmit on frequencies programmed into it with KPG-44D.

Only the Python 3.11+ standard library is used.

Licensed CC-BY-SA 4.0, same as the rest of this repository.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import logging
import os
import queue
import select
import signal
import socketserver
import sys
import termios
import threading
import time
import tty

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None

VERSION = "1.0"
log = logging.getLogger("tkx90-catd")

# ---------------------------------------------------------------------------
# Tone tables
# ---------------------------------------------------------------------------

# Kenwood TM-V7/TM-V71 tone index as used by app_rpt's kenwood_pltocode().
# Index 2 (69.3 Hz) is not produced by app_rpt.
KENWOOD_TONES = {
    1: 67.0, 3: 71.9, 4: 74.4, 5: 77.0, 6: 79.7, 7: 82.5, 8: 85.4, 9: 88.5,
    10: 91.5, 11: 94.8, 12: 97.4, 13: 100.0, 14: 103.5, 15: 107.2, 16: 110.9,
    17: 114.8, 18: 118.8, 19: 123.0, 20: 127.3, 21: 131.8, 22: 136.5,
    23: 141.3, 24: 146.2, 25: 151.4, 26: 156.7, 27: 162.2, 28: 167.9,
    29: 173.8, 30: 179.9, 31: 186.2, 32: 192.8, 33: 203.5, 34: 210.7,
    35: 218.1, 36: 225.7, 37: 233.6, 38: 241.8, 39: 250.3,
}

# Standard EIA CTCSS list (advertised to Hamlib clients).
CTCSS_LIST = [
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4,
    100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8, 136.5,
    141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9, 171.3, 173.8,
    177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5, 206.5,
    210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
]

SHIFT_SIMPLEX, SHIFT_PLUS, SHIFT_MINUS = 0, 1, 2
SHIFT_NAMES = {SHIFT_SIMPLEX: "None", SHIFT_PLUS: "+", SHIFT_MINUS: "-"}
POWER_NAMES = ("low", "med", "high")


def tone_eq(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 0.05


# ---------------------------------------------------------------------------
# Channel plan and matching
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Channel:
    number: int            # Remote Channel 1..15 (0 = front panel)
    name: str
    rx_hz: int
    tx_hz: int
    tx_tone: float | None = None   # CTCSS encode
    rx_tone: float | None = None   # CTCSS decode (programmed in the radio)
    power: str = "high"
    narrow: bool = False
    memory: str | None = None      # app_rpt memory slot (two digits)

    @property
    def shift(self) -> int:
        if self.tx_hz == self.rx_hz:
            return SHIFT_SIMPLEX
        return SHIFT_PLUS if self.tx_hz > self.rx_hz else SHIFT_MINUS

    @property
    def offset_hz(self) -> int:
        return abs(self.tx_hz - self.rx_hz)


@dataclasses.dataclass
class Request:
    rx_hz: int
    shift: int | None = None
    offset_hz: int | None = None
    tx_tone: float | None = None
    tx_tone_given: bool = False
    rx_tone: float | None = None
    rx_tone_given: bool = False
    power: str | None = None

    def tx_hz(self) -> int | None:
        if self.shift is None:
            return None
        if self.shift == SHIFT_SIMPLEX:
            return self.rx_hz
        off = self.offset_hz or 0
        return self.rx_hz + off if self.shift == SHIFT_PLUS else self.rx_hz - off


class ChannelPlan:
    def __init__(self, channels: list[Channel], tolerance_hz: int = 500):
        self.channels = sorted(channels, key=lambda c: c.number)
        self.tolerance_hz = tolerance_hz
        nums = [c.number for c in self.channels]
        if len(nums) != len(set(nums)):
            raise ValueError("duplicate remote channel numbers in plan")
        for c in self.channels:
            if not 1 <= c.number <= 15:
                raise ValueError(f"channel {c.name}: number must be 1..15")

    def by_number(self, n: int) -> Channel | None:
        for c in self.channels:
            if c.number == n:
                return c
        return None

    def candidates(self, rx_hz: int) -> list[Channel]:
        return [c for c in self.channels if abs(c.rx_hz - rx_hz) <= self.tolerance_hz]

    def score(self, c: Channel, req: Request) -> tuple[int, dict]:
        hits = {}
        tx = req.tx_hz()
        hits["tx"] = tx is not None and abs(c.tx_hz - tx) <= self.tolerance_hz
        hits["tx_tone"] = req.tx_tone_given and tone_eq(c.tx_tone, req.tx_tone)
        hits["rx_tone"] = req.rx_tone_given and tone_eq(c.rx_tone, req.rx_tone)
        hits["power"] = req.power is not None and c.power == req.power
        s = 8 * hits["tx"] + 4 * hits["tx_tone"] + 2 * hits["rx_tone"] + hits["power"]
        return s, hits

    def match(self, req: Request, strict: bool = False,
              require: tuple[str, ...] = ()) -> Channel | None:
        """Best channel for `req`, or None.

        The RX frequency must always match. `strict` also requires the TX
        frequency and the TX tone to match. Names in `require` (keys of
        score()'s hit dict) must match too.
        """
        best = None
        best_score = -1
        for c in self.candidates(req.rx_hz):
            s, hits = self.score(c, req)
            if strict and not (hits["tx"] and (hits["tx_tone"] or not req.tx_tone_given)):
                continue
            if any(not hits[r] for r in require):
                continue
            if s > best_score:
                best, best_score = c, s
        return best


def load_config(path: str) -> dict:
    if tomllib is None:
        raise SystemExit("Python 3.11 or newer is required (tomllib)")
    with open(path, "rb") as f:
        return tomllib.load(f)


def plan_from_config(cfg: dict) -> ChannelPlan:
    chans = []
    for i, ch in enumerate(cfg.get("channel", [])):
        rx = int(round(float(ch["rx_mhz"]) * 1e6))
        if "tx_mhz" in ch:
            tx = int(round(float(ch["tx_mhz"]) * 1e6))
        else:
            off = int(round(float(ch.get("offset_khz", 0)) * 1e3))
            shift = ch.get("shift", "simplex")
            tx = {"simplex": rx, "+": rx + off, "-": rx - off}[shift]
        chans.append(Channel(
            number=int(ch["number"]),
            name=str(ch.get("name", f"CH{ch['number']}")),
            rx_hz=rx, tx_hz=tx,
            tx_tone=float(ch["tx_tone"]) if ch.get("tx_tone") else None,
            rx_tone=float(ch["rx_tone"]) if ch.get("rx_tone") else None,
            power=str(ch.get("power", "high")),
            narrow=bool(ch.get("narrow", False)),
            memory=str(ch["memory"]) if "memory" in ch else None,
        ))
    match = cfg.get("match", {})
    return ChannelPlan(chans, int(match.get("tolerance_hz", 500)))


# ---------------------------------------------------------------------------
# Serial helpers
# ---------------------------------------------------------------------------

def set_raw(fd: int, baud: int | None = None) -> None:
    tty.setraw(fd)
    attrs = termios.tcgetattr(fd)
    attrs[2] |= termios.CLOCAL | termios.CREAD
    if baud:
        speed = getattr(termios, f"B{baud}")
        attrs[4] = attrs[5] = speed
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


# ---------------------------------------------------------------------------
# Hardware back ends
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Status:
    channel: int = 0
    busy: bool = False
    tx: bool = False
    ptt: bool = False
    cos: bool = False
    tot: bool = False
    ao: str = "0000"
    rssi_mv: int = 0
    online: bool = False


class BackendError(Exception):
    pass


class SimBackend:
    """In-memory stand-in for the Pico, for testing and dry runs."""

    def __init__(self):
        self.status = Status(online=True)
        self.lock = threading.Lock()
        self.on_reconnect = None
        self.listeners: list = []

    def start(self):
        pass

    def stop(self):
        pass

    def set_channel(self, n: int) -> None:
        with self.lock:
            if self.status.tx:
                raise BackendError("radio is transmitting")
            self.status.channel = n

    def get_status(self) -> Status:
        with self.lock:
            return dataclasses.replace(self.status)

    def command(self, line: str) -> str:
        if line.startswith(("SM ", "MM ")):
            return line
        return "ERR CMD"


class PicoBackend:
    """Talks to the TKX90CTL firmware over USB CDC."""

    def __init__(self, device: str, poll_s: float = 0.25, timeout_s: float = 1.0,
                 poll_cmd: str | None = "ST"):
        self.device = device
        self.poll_s = poll_s
        self.poll_cmd = poll_cmd
        self.listeners: list = []             # callback(line) for HB/EV lines
        self.timeout_s = timeout_s
        self.fd: int | None = None
        self.lock = threading.Lock()          # serialises commands
        self.replies: queue.Queue[str] = queue.Queue()
        self.status = Status()
        self.stop_evt = threading.Event()
        self.on_reconnect = None              # callback(backend)
        self.threads: list[threading.Thread] = []

    # -- life cycle --------------------------------------------------------
    def start(self):
        for target in (self._reader, self._poller):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self.stop_evt.set()
        self._close()

    def _open(self) -> bool:
        try:
            fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            set_raw(fd)
            fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
        except OSError as e:
            log.debug("cannot open %s: %s", self.device, e)
            return False
        self.fd = fd
        log.info("controller connected on %s", self.device)
        return True

    def _close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self.status.online = False

    # -- threads -----------------------------------------------------------
    def _reader(self):
        buf = b""
        while not self.stop_evt.is_set():
            if self.fd is None:
                if not self._open():
                    time.sleep(2)
                    continue
                buf = b""
                threading.Thread(target=self._after_connect, daemon=True).start()
            try:
                r, _, _ = select.select([self.fd], [], [], 0.5)
                if not r:
                    continue
                data = os.read(self.fd, 256)
                if not data:
                    raise OSError("EOF")
            except OSError as e:
                log.warning("controller link lost: %s", e)
                self._close()
                continue
            buf += data
            while b"\n" in buf or b"\r" in buf:
                idx = min(i for i in (buf.find(b"\n"), buf.find(b"\r")) if i >= 0)
                line, buf = buf[:idx].decode(errors="replace").strip(), buf[idx + 1:]
                if line:
                    self._dispatch(line)

    def _dispatch(self, line: str):
        if line.startswith("HB ") and line.split()[1] in ("rx", "tx", "coll", "drop", "auto"):
            for cb in self.listeners:
                cb(line)
            return
        if line.startswith(("EV ", "U2R ", "TRD ")):
            log.info("controller: %s", line)
            for cb in self.listeners:
                cb(line)
            return
        if line.startswith("ID ") and self.replies.empty() and not self.lock.locked():
            log.info("controller says %s", line)
            return
        self.replies.put(line)

    def _after_connect(self):
        time.sleep(0.5)
        try:
            ident = self.command("ID")
            log.info("controller identifies as %r", ident)
        except BackendError:
            pass
        if self.on_reconnect:
            self.on_reconnect(self)

    def _poller(self):
        while not self.stop_evt.is_set():
            if self.fd is not None and self.poll_cmd:
                try:
                    self._parse_status(self.command(self.poll_cmd))
                except BackendError as e:
                    log.debug("status poll failed: %s", e)
            time.sleep(self.poll_s)

    # -- commands ------------------------------------------------------------
    def command(self, line: str) -> str:
        with self.lock:
            if self.fd is None:
                raise BackendError("controller offline")
            while not self.replies.empty():
                self.replies.get_nowait()
            try:
                os.write(self.fd, (line + "\r").encode())
            except OSError as e:
                raise BackendError(str(e)) from e
            try:
                return self.replies.get(timeout=self.timeout_s)
            except queue.Empty:
                raise BackendError(f"no reply to {line!r}") from None

    def _parse_status(self, line: str):
        if not line.startswith("ST "):
            return
        kv = dict(p.split("=", 1) for p in line[3:].split() if "=" in p)
        st = Status(
            channel=int(kv.get("ch", 0)),
            busy=kv.get("sq") == "1",
            tx=kv.get("tx") == "1",
            ptt=kv.get("ptt") == "1",
            cos=kv.get("cos") == "1",
            tot=kv.get("tot") == "1",
            ao=kv.get("ao", "0000"),
            rssi_mv=int(kv.get("rssi", 0)),
            online=True,
        )
        self.status = st

    def set_channel(self, n: int) -> None:
        reply = self.command(f"CH {n}")
        if reply == "ERR TX":
            raise BackendError("radio is transmitting")
        if reply != f"CH {n}":
            raise BackendError(f"unexpected reply {reply!r}")
        self.status.channel = n

    def get_status(self) -> Status:
        return dataclasses.replace(self.status)


# ---------------------------------------------------------------------------
# Radio model shared by the front ends
# ---------------------------------------------------------------------------

class Radio:
    def __init__(self, plan: ChannelPlan, backend, cfg: dict):
        self.plan = plan
        self.backend = backend
        self.lock = threading.RLock()
        mcfg = cfg.get("match", {})
        self.strict = mcfg.get("mode", "best") == "strict"
        rcfg = cfg.get("rssi", {})
        self.rssi_cal = sorted(rcfg.get("cal", [[300, -120.0], [1400, -95.0]]))
        self.s9_dbm = float(rcfg.get("s9_dbm", -93.0))
        self.status_file = cfg.get("daemon", {}).get("status_file")
        self.current: Channel | None = None
        self.power_pref: str | None = None
        boot = int(cfg.get("daemon", {}).get("startup_channel", 0))
        self._startup = self.plan.by_number(boot) if boot else None
        backend.on_reconnect = self._reapply

    def _reapply(self, backend):
        with self.lock:
            target = self.current or self._startup
            if target is None:
                return
            try:
                backend.set_channel(target.number)
                self.current = target
                log.info("selected %s (remote channel %d)", target.name, target.number)
            except BackendError as e:
                log.warning("could not restore channel: %s", e)
            self._write_status()

    def select(self, ch: Channel) -> None:
        with self.lock:
            if self.current is not ch:
                self.backend.set_channel(ch.number)
                log.info("QSY -> %s (remote channel %d, %.5f MHz)",
                         ch.name, ch.number, ch.rx_hz / 1e6)
            self.current = ch
            self._write_status()

    def apply(self, req: Request, strict: bool | None = None,
              require: tuple[str, ...] = ()) -> Channel:
        with self.lock:
            if req.power is None:
                req.power = self.power_pref
            ch = self.plan.match(req, self.strict if strict is None else strict, require)
            if ch is None:
                raise LookupError(f"{req.rx_hz/1e6:.5f} MHz is not in the channel plan")
            self.select(ch)
            return ch

    def request_from_current(self) -> Request:
        c = self.current
        if c is None:
            return Request(rx_hz=0)
        return Request(rx_hz=c.rx_hz, shift=c.shift, offset_hz=c.offset_hz,
                       tx_tone=c.tx_tone, tx_tone_given=True,
                       rx_tone=c.rx_tone, rx_tone_given=True, power=c.power)

    def rssi_dbm(self, mv: int) -> float:
        cal = self.rssi_cal
        if len(cal) < 2:
            return -130.0
        if mv <= cal[0][0]:
            (x0, y0), (x1, y1) = cal[0], cal[1]
        elif mv >= cal[-1][0]:
            (x0, y0), (x1, y1) = cal[-2], cal[-1]
        else:
            for (x0, y0), (x1, y1) in zip(cal, cal[1:]):
                if x0 <= mv <= x1:
                    break
        if x1 == x0:
            return float(y0)
        return y0 + (mv - x0) * (y1 - y0) / (x1 - x0)

    def _write_status(self):
        if not self.status_file:
            return
        st = self.backend.get_status()
        c = self.current
        data = {
            "channel": c.number if c else 0,
            "name": c.name if c else None,
            "rx_mhz": c.rx_hz / 1e6 if c else None,
            "tx_mhz": c.tx_hz / 1e6 if c else None,
            "tx_tone": c.tx_tone if c else None,
            "rx_tone": c.rx_tone if c else None,
            "online": st.online,
            "time": int(time.time()),
        }
        try:
            tmp = self.status_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.status_file)
        except OSError as e:
            log.debug("status file: %s", e)


# ---------------------------------------------------------------------------
# Front end 1: app_rpt "remote = kenwood" emulation on a pseudo-terminal
# ---------------------------------------------------------------------------

def parse_vw(args: str) -> Request:
    """Parse the argument string of app_rpt's VW command.

    app_rpt (rpt_rig.c, setkenwood) formats it as
      "VW %c,%05d%s,%d,%d,0,%d,%d,,%02d,,%02d,%s"
       band, MHz+kHz, step, shift, reverse, tone, ctcss, dcs,
       tone#, dcs#, ctcss#, offset(Hz)
    """
    f = [x.strip() for x in args.split(",")]
    if len(f) < 12:
        raise ValueError("short VW")
    digits = f[1]
    mhz = int(digits[:5])
    frac = digits[5:] or "0"
    rx_hz = mhz * 1_000_000 + int(frac) * 10 ** (6 - len(frac))
    shift = int(f[3])
    tone_on = f[5] == "1"
    ctcss_on = f[6] == "1"
    tx_code = int(f[8]) if f[8] not in ("", "-1") else None
    rx_code = int(f[10]) if f[10] not in ("", "-1") else None
    offset = int(f[11]) if f[11] else 0
    if tone_on and tx_code not in KENWOOD_TONES:
        raise ValueError("unsupported TX tone code")
    if ctcss_on and rx_code not in KENWOOD_TONES:
        raise ValueError("unsupported RX tone code")
    return Request(
        rx_hz=rx_hz,
        shift=shift if shift in SHIFT_NAMES else SHIFT_SIMPLEX,
        offset_hz=offset,
        tx_tone=KENWOOD_TONES[tx_code] if tone_on else None, tx_tone_given=True,
        rx_tone=KENWOOD_TONES[rx_code] if ctcss_on else None, rx_tone_given=True,
    )


class KenwoodEmulator:
    """Answers the commands app_rpt sends to a 'kenwood' remote rig."""

    def __init__(self, radio: Radio):
        self.radio = radio
        self.pending: Request | None = None

    def handle(self, line: str) -> str:
        line = line.strip()
        if not line:
            return ""
        cmd, _, args = line.partition(" ")
        cmd = cmd.upper()
        try:
            if cmd == "ID":
                return "ID TK-X90"
            if cmd in ("VMC", "BC", "RBN", "VM"):
                return line
            if cmd == "VW":
                req = parse_vw(args)
                self.pending = req
                self.radio.apply(req)
                return line
            if cmd == "PC":
                # app_rpt: powers[] = {2,1,0} for low/med/high
                code = int(args.split(",")[-1])
                self.radio.power_pref = {0: "high", 1: "med", 2: "low"}.get(code)
                if self.pending is not None:
                    try:
                        self.radio.apply(dataclasses.replace(self.pending))
                    except LookupError:
                        pass
                return line
        except (LookupError, ValueError, BackendError) as e:
            log.warning("app_rpt %r rejected: %s", line, e)
            return "N"
        return "?"


class KenwoodPty:
    """Creates a pty and publishes its slave side at a stable path."""

    def __init__(self, emulator: KenwoodEmulator, link_path: str,
                 group: str | None = None, mode: int = 0o660):
        self.emu = emulator
        self.link_path = link_path
        self.group = group
        self.mode = mode
        self.master = self.slave = None
        self.stop_evt = threading.Event()

    def start(self):
        self.master, self.slave = os.openpty()
        set_raw(self.slave, 9600)
        # Make the pty the controlling terminal of *our* session so that
        # Asterisk (which opens ioport without O_NOCTTY) cannot adopt it,
        # and never gets a SIGHUP if this daemon restarts.
        try:
            os.setsid()
        except OSError:
            pass
        try:
            fcntl.ioctl(self.slave, termios.TIOCSCTTY, 0)
        except OSError as e:
            log.debug("TIOCSCTTY: %s", e)
        name = os.ttyname(self.slave)
        os.chmod(name, self.mode)
        if self.group:
            import grp
            try:
                os.chown(name, -1, grp.getgrnam(self.group).gr_gid)
            except (KeyError, PermissionError) as e:
                log.warning("cannot chgrp %s to %s: %s", name, self.group, e)
        os.makedirs(os.path.dirname(self.link_path) or ".", exist_ok=True)
        try:
            os.unlink(self.link_path)
        except FileNotFoundError:
            pass
        os.symlink(name, self.link_path)
        log.info("app_rpt CAT port: %s -> %s", self.link_path, name)
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stop_evt.set()
        try:
            if os.path.islink(self.link_path):
                os.unlink(self.link_path)
        except OSError:
            pass

    def _run(self):
        buf = b""
        while not self.stop_evt.is_set():
            r, _, _ = select.select([self.master], [], [], 0.5)
            if not r:
                continue
            try:
                data = os.read(self.master, 256)
            except OSError:
                time.sleep(0.2)
                continue
            buf += data
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                text = line.decode(errors="replace").strip("\n ")
                log.debug("app_rpt -> %r", text)
                reply = self.emu.handle(text)
                if reply:
                    log.debug("app_rpt <- %r", reply)
                    os.write(self.master, (reply + "\r").encode())
            if len(buf) > 512:
                buf = b""


# ---------------------------------------------------------------------------
# Front end 2: Hamlib rigctld network protocol
# ---------------------------------------------------------------------------

RIG_OK, RIG_EINVAL, RIG_EIO, RIG_EPROTO, RIG_ENAVAIL, RIG_EACCESS = 0, -1, -6, -8, -11, -22
MODE_FM = 0x20
VFO_A = 0x1
LEVEL_RAWSTR = 1 << 26
LEVEL_STRENGTH = 1 << 30
FUNC_TONE = 1 << 4
FUNC_TSQL = 1 << 5

LONG_TO_SHORT = {
    "get_freq": "f", "set_freq": "F", "get_mode": "m", "set_mode": "M",
    "get_vfo": "v", "set_vfo": "V", "get_ptt": "t", "set_ptt": "T",
    "get_rptr_shift": "r", "set_rptr_shift": "R",
    "get_rptr_offs": "o", "set_rptr_offs": "O",
    "get_ctcss_tone": "c", "set_ctcss_tone": "C",
    "get_level": "l", "set_level": "L", "get_func": "u", "set_func": "U",
    "get_mem": "e", "set_mem": "E", "get_info": "_", "quit": "q",
    "get_split_vfo": "s", "set_split_vfo": "S",
}
SHORT_TO_LONG = {v: k for k, v in LONG_TO_SHORT.items()}


class RigctlSession:
    """Implements the subset of rigctld commands Hamlib clients use."""

    def __init__(self, radio: Radio, cfg: dict):
        self.radio = radio
        rng = cfg.get("hamlib", {})
        self.rx_lo = int(float(rng.get("rx_low_mhz", 136.0)) * 1e6)
        self.rx_hi = int(float(rng.get("rx_high_mhz", 174.0)) * 1e6)
        self.tx_lo = int(float(rng.get("tx_low_mhz", self.rx_lo / 1e6)) * 1e6)
        self.tx_hi = int(float(rng.get("tx_high_mhz", self.rx_hi / 1e6)) * 1e6)
        self.read_only = bool(rng.get("read_only", False))
        self.pending_offset: int | None = None

    # -- helpers -------------------------------------------------------------
    def _apply(self, req: Request, require=()):
        if self.read_only:
            return RIG_EACCESS
        try:
            self.radio.apply(req, strict=False, require=require)
            return RIG_OK
        except LookupError as e:
            log.info("hamlib: %s", e)
            return RIG_EINVAL
        except BackendError as e:
            log.warning("hamlib: %s", e)
            return RIG_EIO

    def dump_state(self) -> list[str]:
        lines = [
            "1",            # protocol version
            "1",            # rig model (ignored by netrigctl)
            "0",            # deprecated ITU region
            f"{self.rx_lo} {self.rx_hi} 0x{MODE_FM:x} -1 -1 0x{VFO_A:x} 0x0",
            "0 0 0 0 0 0 0",
            f"{self.tx_lo} {self.tx_hi} 0x{MODE_FM:x} 5000 50000 0x{VFO_A:x} 0x0",
            "0 0 0 0 0 0 0",
            f"0x{MODE_FM:x} 2500",
            "0 0",
            f"0x{MODE_FM:x} 15000",
            f"0x{MODE_FM:x} 10000",
            "0 0",
            "0", "0", "0", "0",       # max_rit, max_xit, max_ifshift, announces
            "",                        # preamp list
            "",                        # attenuator list
            f"0x{FUNC_TONE | FUNC_TSQL:x}",
            f"0x{FUNC_TONE | FUNC_TSQL:x}",
            f"0x{LEVEL_RAWSTR | LEVEL_STRENGTH:x}",
            "0x0", "0x0", "0x0",
            "vfo_ops=0x0",
            "ptt_type=0x0",
            "targetable_vfo=0x0",
            "has_set_vfo=0",
            "has_get_vfo=0",
            "has_set_freq=1",
            "has_get_freq=1",
            "has_set_conf=0",
            "has_get_conf=0",
            "has_power2mW=0",
            "has_mW2power=0",
            "timeout=2000",
            "rig_model=2",
            f"rigctld_version=tkx90-catd {VERSION}",
            "ctcss_list=" + "".join(f" {t:.1f}" for t in CTCSS_LIST),
            "done",
        ]
        return lines

    # -- command execution ---------------------------------------------------------
    def execute(self, line: str) -> tuple[list[str], bool]:
        """Return (response lines, keep_open)."""
        line = line.strip()
        if not line:
            return [], True
        ext = None
        if line[0] in "+;|,":
            ext, line = line[0], line[1:].lstrip()
        if line.startswith("\\"):
            name, _, rest = line[1:].partition(" ")
            cmd = LONG_TO_SHORT.get(name, "\\" + name)
        else:
            cmd, rest = line[0], line[1:].strip()
        args = rest.split()
        long_name = SHORT_TO_LONG.get(cmd, cmd.lstrip("\\"))
        try:
            values, rc = self._run(cmd, args)
        except (ValueError, IndexError):
            values, rc = [], RIG_EINVAL
        if cmd == "q":
            return [], False
        if ext is None:
            if values is None:
                return [f"RPRT {rc}"], True
            if rc != RIG_OK:
                return [f"RPRT {rc}"], True
            return [str(v) for v in values] if values else ["RPRT 0"], True
        # Extended response
        out = [f"{long_name}: {' '.join(args)}".rstrip()]
        for v in values or []:
            out.append(str(v))
        out.append(f"RPRT {rc}")
        if ext != "+":
            return [ext.join(out)], True
        return out, True

    def _run(self, cmd: str, a: list[str]):
        r = self.radio
        cur = r.current
        if cmd == "\\chk_vfo":
            return ["0"], RIG_OK
        if cmd == "\\dump_state":
            return self.dump_state(), RIG_OK
        if cmd in ("\\get_powerstat",):
            return ["1"], RIG_OK
        if cmd == "\\get_dcd":
            return ["1" if r.backend.get_status().busy else "0"], RIG_OK
        if cmd == "_":
            return [f"Kenwood TK-x90 via tkx90-catd {VERSION}"], RIG_OK
        if cmd == "f":
            return [str(cur.rx_hz if cur else 0)], RIG_OK
        if cmd == "F":
            hz = int(float(a[0]))
            base = r.request_from_current()
            # When the frequency changes, ask only for the frequency.
            # The best-scoring channel brings its own shift and tones.
            req = Request(rx_hz=hz, shift=base.shift, offset_hz=base.offset_hz,
                          tx_tone=base.tx_tone, tx_tone_given=base.tx_tone_given,
                          rx_tone=base.rx_tone, rx_tone_given=base.rx_tone_given)
            return None, self._apply(req)
        if cmd == "m":
            return ["FM", str(12500 if cur and cur.narrow else 15000)], RIG_OK
        if cmd == "M":
            return None, RIG_OK if a and a[0].upper() in ("FM", "FMN") else RIG_EINVAL
        if cmd == "v":
            return ["VFOA"], RIG_OK
        if cmd == "V":
            return None, RIG_OK
        if cmd == "s":
            return ["0", "VFOA"], RIG_OK
        if cmd == "S":
            return None, RIG_OK if a and a[0] == "0" else RIG_ENAVAIL
        if cmd == "t":
            return ["1" if r.backend.get_status().tx else "0"], RIG_OK
        if cmd == "T":
            # PTT belongs to app_rpt / the URI; never key from here.
            return None, RIG_ENAVAIL
        if cmd == "r":
            return [SHIFT_NAMES[cur.shift] if cur else "None"], RIG_OK
        if cmd == "R":
            want = {"+": SHIFT_PLUS, "-": SHIFT_MINUS, "None": SHIFT_SIMPLEX,
                    "0": SHIFT_SIMPLEX}.get(a[0] if a else "None", SHIFT_SIMPLEX)
            req = r.request_from_current()
            req.shift = want
            if want == SHIFT_SIMPLEX:
                req.offset_hz = 0
            elif self.pending_offset:
                req.offset_hz = self.pending_offset
            elif not req.offset_hz:
                req.offset_hz = 600_000 if req.rx_hz < 300e6 else 5_000_000
            return None, self._apply(req, require=("tx",))
        if cmd == "o":
            return [str(cur.offset_hz if cur else 0)], RIG_OK
        if cmd == "O":
            req = r.request_from_current()
            req.offset_hz = abs(int(float(a[0])))
            if req.shift in (None, SHIFT_SIMPLEX):
                # Remember it for a following set_rptr_shift.
                self.pending_offset = req.offset_hz or None
                return None, RIG_OK
            return None, self._apply(req, require=("tx",))
        if cmd == "c":
            t = cur.tx_tone if cur else None
            return [str(int(round(t * 10)) if t else 0)], RIG_OK
        if cmd == "C":
            tenth = int(float(a[0]))
            req = r.request_from_current()
            req.tx_tone = tenth / 10.0 if tenth else None
            req.tx_tone_given = True
            return None, self._apply(req, require=("tx_tone",))
        if cmd in ("\\get_ctcss_sql",):
            t = cur.rx_tone if cur else None
            return [str(int(round(t * 10)) if t else 0)], RIG_OK
        if cmd in ("\\set_ctcss_sql",):
            tenth = int(float(a[0]))
            req = r.request_from_current()
            req.rx_tone = tenth / 10.0 if tenth else None
            req.rx_tone_given = True
            return None, self._apply(req, require=("rx_tone",))
        if cmd == "u":
            func = a[0].upper()
            if func == "TONE":
                return ["1" if cur and cur.tx_tone else "0"], RIG_OK
            if func == "TSQL":
                return ["1" if cur and cur.rx_tone else "0"], RIG_OK
            return None, RIG_ENAVAIL
        if cmd == "U":
            func, val = a[0].upper(), a[1] == "1"
            req = r.request_from_current()
            if func == "TONE":
                if not val:
                    req.tx_tone = None
                elif req.tx_tone is None:
                    return None, RIG_EINVAL
                return None, self._apply(req, require=("tx_tone",))
            if func == "TSQL":
                if not val:
                    req.rx_tone = None
                elif req.rx_tone is None:
                    # Tone squelch on the same tone we encode (the usual
                    # repeater setup).
                    if req.tx_tone is None:
                        return None, RIG_EINVAL
                    req.rx_tone = req.tx_tone
                return None, self._apply(req, require=("rx_tone",))
            return None, RIG_ENAVAIL
        if cmd == "l":
            lvl = a[0].upper()
            st = r.backend.get_status()
            if lvl == "RAWSTR":
                return [str(st.rssi_mv)], RIG_OK
            if lvl == "STRENGTH":
                return [str(int(round(r.rssi_dbm(st.rssi_mv) - r.s9_dbm)))], RIG_OK
            return None, RIG_ENAVAIL
        if cmd == "L":
            return None, RIG_ENAVAIL
        if cmd == "e":
            return [str(cur.number if cur else 0)], RIG_OK
        if cmd == "E":
            if self.read_only:
                return None, RIG_EACCESS
            ch = r.plan.by_number(int(a[0]))
            if ch is None:
                return None, RIG_EINVAL
            try:
                r.select(ch)
            except BackendError:
                return None, RIG_EIO
            return None, RIG_OK
        if cmd == "q":
            return None, RIG_OK
        return None, RIG_ENAVAIL


class RigctlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, radio: Radio, cfg: dict):
        self.radio = radio
        self.cfg = cfg
        super().__init__(addr, RigctlHandler)


class RigctlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        sess = RigctlSession(self.server.radio, self.server.cfg)
        peer = self.client_address
        log.info("hamlib client %s connected", peer)
        try:
            for raw in self.rfile:
                text = raw.decode(errors="replace")
                lines, keep = sess.execute(text)
                log.debug("hamlib %r -> %r", text.strip(), lines[:3])
                if lines:
                    self.wfile.write(("\n".join(lines) + "\n").encode())
                    self.wfile.flush()
                if not keep:
                    break
        except (ConnectionError, OSError):
            pass
        log.info("hamlib client %s disconnected", peer)


# ---------------------------------------------------------------------------
# app_rpt helpers
# ---------------------------------------------------------------------------

def asl_memory_stanza(plan: ChannelPlan, stanza: str = "memory-tkx90") -> str:
    """Render the plan as an rpt.conf [memory] stanza for remote,1."""
    out = [f"[{stanza}]", "; generated by tkx90-catd --asl-memory"]
    for c in plan.channels:
        slot = c.memory or f"{c.number:02d}"
        tone = c.tx_tone or c.rx_tone or 100.0
        mhz = f"{c.rx_hz / 1e6:.3f}"
        flags = {"low": "l", "med": "m", "high": "h"}.get(c.power, "m")
        flags += {SHIFT_SIMPLEX: "s", SHIFT_PLUS: "+", SHIFT_MINUS: "-"}[c.shift]
        if c.tx_tone:
            flags += "t"
        if c.rx_tone:
            flags += "r"
        split = ""
        default = 600 if c.rx_hz < 300e6 else 5000
        if c.shift != SHIFT_SIMPLEX and c.offset_hz != default * 1000:
            split = f"{c.offset_hz // 1000},"
        out.append(f"{slot} = {mhz},{tone:.1f},{split}{flags}   ; {c.name}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-c", "--config", default="/etc/tkx90/tkx90.toml")
    ap.add_argument("--sim", action="store_true", help="use the simulated controller")
    ap.add_argument("--check", action="store_true", help="validate config and exit")
    ap.add_argument("--asl-memory", action="store_true",
                    help="print an rpt.conf [memory] stanza for the plan and exit")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config(args.config)
    plan = plan_from_config(cfg)

    if args.check:
        for c in plan.channels:
            print(f"{c.number:2d} {c.name:<16} RX {c.rx_hz/1e6:10.5f}  TX {c.tx_hz/1e6:10.5f}  "
                  f"T {c.tx_tone or '-'}  TSQ {c.rx_tone or '-'}  {c.power}")
        return 0
    if args.asl_memory:
        sys.stdout.write(asl_memory_stanza(plan))
        return 0

    dcfg = cfg.get("daemon", {})
    if args.sim or dcfg.get("controller") == "sim":
        backend = SimBackend()
    else:
        backend = PicoBackend(dcfg.get("controller", "/dev/tkx90ctl"),
                              poll_s=float(dcfg.get("poll_s", 0.25)))
    radio = Radio(plan, backend, cfg)

    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    acfg = cfg.get("app_rpt", {})
    pty = None
    if acfg.get("enabled", True):
        pty = KenwoodPty(KenwoodEmulator(radio), acfg.get("link", "/run/tkx90/asl-cat"),
                         acfg.get("group", "dialout"))
        pty.start()

    server = None
    hcfg = cfg.get("hamlib", {})
    if hcfg.get("enabled", True):
        addr = (hcfg.get("listen", "127.0.0.1"), int(hcfg.get("port", 4532)))
        server = RigctlServer(addr, radio, cfg)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("rigctld protocol on %s:%d", *addr)

    vch_parts = []
    if cfg.get("vch", {}).get("enabled", False):
        import vch
        vch_parts = vch.start_from_config(cfg, radio, backend)

    backend.start()
    if isinstance(backend, SimBackend):
        radio._reapply(backend)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()

    if server:
        server.shutdown()
    for part in vch_parts:
        part.stop()
    if pty:
        pty.stop()
    backend.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
