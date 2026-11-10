#!/usr/bin/env python3
"""vch -- virtual control head for the Kenwood TK-690/790/890.

Replaces or mirrors the KCH-10/KCH-11 front panel with a web panel, at
one of three levels:

  Level 1, "dbonly" (works with no learning):
      Uses only the documented DB-25 features through the TKX90CTL
      controller and tkx90-catd: channel up/down through the Remote
      Channel plan, BUSY/TX LEDs from SQ/TXS, S-meter from RSSI, speaker
      mute.  The display shows the plan's channel name.

  Level 2, "tap" (real head stays on the radio):
      The controller also sits on the head bus (TRD) through J3 or the
      front 12-pin connector.  Learned frames let the panel mirror the
      real head's display and press its keys (PF1-PF9, MON, SCN,
      GR up/down, channel knob ...).

  Level 3, "bridge" (inline on the W501 head cable):
      A second Pico running firmware/vch_bridge.py splits TRD between
      radio and head.  Frames have a known direction, and the radio can
      run with the head removed ("headless"), with learned answers
      served by the bridge's auto-responder.  It can also press POWER.

The radio<->head protocol is not documented.  Levels 2 and 3 run from a
*codebook* learned from your own radio with the `learn` commands below.
Nothing in this file guesses at message formats.

Command line (talks to the Pico directly; stop tkx90-catd first if it
owns the same device):

  vch.py -d /dev/tkx90ctl --tap capture -s 30 -o capture.jsonl
  vch.py -d /dev/tkx90ctl --tap learn-baseline -s 20
  vch.py -d /dev/tkx90ctl --tap learn-key PF1
  vch.py -d /dev/tkx90ctl --tap learn-display "RPT 147.03"
  vch.py -d /dev/tkx90vch --bridge learn-auto -s 60
  vch.py show

Licensed CC-BY-SA 4.0, same as the rest of this repository.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import http.server
import json
import logging
import os
import queue
import sys
import threading
import time
import urllib.parse

log = logging.getLogger("tkx90-vch")
HERE = os.path.dirname(os.path.abspath(__file__))

# Keys of a KCH-11 (service manual, display unit IC4 terminal list).
# KCH-10 has PF1-PF5 only.  CH_UP / CH_DN are the channel encoder,
# GR_UP / GR_DN the group keys.
KCH11_KEYS = ["PF1", "PF2", "PF3", "PF4", "PF5", "PF6", "PF7", "PF8", "PF9",
              "MON", "SCN", "GR_UP", "GR_DN", "CH_UP", "CH_DN"]


# ---------------------------------------------------------------------------
# Frames and codebook
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Frame:
    kind: str     # rx | tx | coll | drop | auto
    src: str      # D (radio) | H (head) | ? (tap: unknown)
    t: int        # controller milliseconds (24-bit)
    data: bytes

    @property
    def hex(self) -> str:
        return self.data.hex()

    @classmethod
    def parse(cls, line: str) -> "Frame | None":
        p = line.split()
        if len(p) != 5 or p[0] != "HB":
            return None
        try:
            return cls(p[1], p[2], int(p[3]), bytes.fromhex(p[4]))
        except ValueError:
            return None


@dataclasses.dataclass
class DisplayRule:
    field: str           # "main" (14-char) or "sub" (3-digit) or anything
    prefix: str          # hex prefix that identifies the frame
    offset: int          # byte offset of the text
    length: int
    encoding: str = "ascii"


class Codebook:
    def __init__(self, path: str | None = None):
        self.path = path
        self.keys: dict[str, list[dict]] = {}     # name -> [{"dt": ms, "hex": ..}]
        self.auto: list[dict] = []                # [{"match": hex, "reply": hex}]
        self.display: list[DisplayRule] = []
        self.ignore: set[str] = set()             # baseline frames
        self.notes = ""
        if path and os.path.exists(path):
            self.load(path)

    def load(self, path):
        with open(path) as f:
            d = json.load(f)
        self.keys = d.get("keys", {})
        self.auto = d.get("auto", [])
        self.display = [DisplayRule(**r) for r in d.get("display", [])]
        self.ignore = set(d.get("ignore", []))
        self.notes = d.get("notes", "")

    def save(self, path=None):
        path = path or self.path
        d = {
            "version": 1,
            "notes": self.notes,
            "keys": self.keys,
            "auto": self.auto,
            "display": [dataclasses.asdict(r) for r in self.display],
            "ignore": sorted(self.ignore),
        }
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, path)

    def decode(self, data: bytes) -> dict[str, str]:
        out = {}
        h = data.hex()
        for r in self.display:
            if h.startswith(r.prefix) and len(data) >= r.offset + r.length:
                raw = data[r.offset:r.offset + r.length]
                out[r.field] = raw.decode(r.encoding, errors="replace")
        return out


# ---------------------------------------------------------------------------
# Learning (pure functions over captured frames, easy to test)
# ---------------------------------------------------------------------------

def frames_after(frames: list[Frame], ignore: set[str], want_src=("H", "?")) -> list[Frame]:
    return [f for f in frames if f.kind == "rx" and f.src in want_src and f.hex not in ignore]


def learn_key_sequence(frames: list[Frame], ignore: set[str], bridge: bool) -> list[dict]:
    """Turn the new frames seen while one key was pressed into a replayable
    sequence [{"dt": ms_since_first, "hex": ...}].  In bridge mode only
    head-originated frames count."""
    new = frames_after(frames, ignore, ("H",) if bridge else ("?", "H"))
    if not new:
        return []
    t0 = new[0].t
    seq = []
    for f in new:
        dt = (f.t - t0) & 0xFFFFFF
        if seq and seq[-1]["hex"] == f.hex and dt - seq[-1]["dt"] < 30:
            continue   # repeated copy of the same frame
        seq.append({"dt": dt, "hex": f.hex})
    return seq


def find_text(frames: list[Frame], text: str, field: str,
              want_src=("D", "?")) -> DisplayRule | None:
    """Locate `text` inside captured radio->head frames and build a rule
    (frame prefix + offset).  Tries the text as typed and upper-cased,
    and space-padded to the 14-character KCH-11 width."""
    cands = []
    for t in {text, text.upper(), text.ljust(14), text.upper().ljust(14)}:
        cands.append(t.encode("ascii", "replace"))
    hits = collections.Counter()
    for f in frames:
        if f.kind != "rx" or f.src not in want_src:
            continue
        for c in cands:
            i = f.data.find(c)
            if i >= 0:
                prefix = f.data[:min(i, 2)].hex()
                hits[(prefix, i, len(c))] += 1
                break
    if not hits:
        return None
    (prefix, off, ln), _ = hits.most_common(1)[0]
    return DisplayRule(field=field, prefix=prefix, offset=off, length=ln)


def learn_auto_pairs(frames: list[Frame], window_ms: int = 60) -> list[dict]:
    """From a bridge capture with the real head attached, pair each radio
    frame with the head frame that answered it within `window_ms`, and
    keep the most common answer for each radio frame."""
    answers: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    last_d = None
    for f in frames:
        if f.kind != "rx":
            continue
        if f.src == "D":
            last_d = f
        elif f.src == "H" and last_d is not None:
            if ((f.t - last_d.t) & 0xFFFFFF) <= window_ms:
                answers[last_d.hex][f.hex] += 1
            last_d = None
    out = []
    for match, c in answers.items():
        reply, n = c.most_common(1)[0]
        out.append({"match": match, "reply": reply, "seen": n})
    out.sort(key=lambda r: -r["seen"])
    return out


# ---------------------------------------------------------------------------
# Virtual head state
# ---------------------------------------------------------------------------

class VirtualHead:
    """Shared state behind the web panel."""

    def __init__(self, mode: str, codebook: Codebook, radio=None,
                 ctl=None, hb_link=None, allow_power=False):
        self.mode = mode                    # dbonly | tap | bridge
        self.cb = codebook
        self.radio = radio                  # tkx90_catd.Radio (optional)
        self.ctl = ctl                      # DB-25 controller backend
        self.hb = hb_link                   # backend carrying HB frames
        self.allow_power = allow_power
        self.lock = threading.Lock()
        self.display = {"main": "", "sub": ""}
        self.frames: collections.deque[Frame] = collections.deque(maxlen=2000)
        self.subscribers: list[queue.Queue] = []
        self.spk_mute = False
        self.last_event = ""
        if hb_link is not None:
            hb_link.listeners.append(self.on_line)

    # -- frames from the bus --------------------------------------------------
    def on_line(self, line: str):
        f = Frame.parse(line)
        if f is None:
            self.last_event = line
            self.notify()
            return
        with self.lock:
            self.frames.append(f)
            if f.kind == "rx" and f.src in ("D", "?"):
                dec = self.cb.decode(f.data)
                if dec:
                    self.display.update(dec)
        self.notify()

    # -- actions ----------------------------------------------------------------------
    def press(self, key: str) -> tuple[bool, str]:
        key = key.upper()
        if key == "POWER":
            if self.mode != "bridge" or not self.allow_power:
                return False, "POWER needs the inline bridge and allow_power = true"
            return self._cmd(self.hb, "PWR 300")
        if key == "SP":
            self.spk_mute = not self.spk_mute
            return self._cmd(self.ctl, f"SM {1 if self.spk_mute else 0}")
        seq = self.cb.keys.get(key)
        if seq and self.mode in ("tap", "bridge"):
            threading.Thread(target=self._replay, args=(seq,), daemon=True).start()
            return True, f"sent {len(seq)} frame(s)"
        if key in ("CH_UP", "CH_DN") and self.radio is not None:
            return self._step_channel(+1 if key == "CH_UP" else -1)
        return False, f"{key} is not learned (see vch.py learn-key)"

    def select_channel(self, n: int) -> tuple[bool, str]:
        if self.radio is None:
            return False, "no radio"
        ch = self.radio.plan.by_number(n)
        if ch is None:
            return False, f"remote channel {n} is not in the plan"
        try:
            self.radio.select(ch)
        except Exception as e:  # BackendError
            return False, str(e)
        self.notify()
        return True, ch.name

    def _step_channel(self, step):
        chans = self.radio.plan.channels
        if not chans:
            return False, "empty plan"
        cur = self.radio.current
        i = chans.index(cur) if cur in chans else -1
        return self.select_channel(chans[(i + step) % len(chans)].number)

    def _replay(self, seq):
        start = time.monotonic()
        for item in seq:
            delay = item["dt"] / 1000.0 - (time.monotonic() - start)
            if delay > 0:
                time.sleep(delay)
            self._cmd(self.hb, f"HBTX D {item['hex']}")

    def _cmd(self, link, line):
        if link is None:
            return False, "no controller"
        try:
            r = link.command(line)
        except Exception as e:
            return False, str(e)
        ok = not r.startswith("ERR")
        return ok, r

    # -- state -------------------------------------------------------------
    def state(self) -> dict:
        st = None
        if self.ctl is not None:
            try:
                st = self.ctl.get_status()
            except Exception:
                st = None
        cur = self.radio.current if self.radio else None
        with self.lock:
            main = self.display.get("main") or ""
            sub = self.display.get("sub") or ""
        level = {"dbonly": 1, "tap": 2, "bridge": 3}[self.mode]
        if not main and cur is not None:
            main = cur.name
        if not sub and cur is not None:
            sub = f"{cur.number:3d}"
        s = {
            "level": level,
            "mode": self.mode,
            "main": main[:14],
            "sub": sub[-3:] if sub else "",
            "busy": bool(st and st.busy),
            "tx": bool(st and st.tx),
            "online": bool(st and st.online),
            "rssi_dbm": round(self.radio.rssi_dbm(st.rssi_mv), 1) if (st and self.radio) else None,
            "spk_mute": self.spk_mute,
            "channel": cur.number if cur else 0,
            "rx_mhz": cur.rx_hz / 1e6 if cur else None,
            "tx_mhz": cur.tx_hz / 1e6 if cur else None,
            "tx_tone": cur.tx_tone if cur else None,
            "rx_tone": cur.rx_tone if cur else None,
            "learned_keys": sorted(self.cb.keys),
            "plan": [{"n": c.number, "name": c.name} for c in self.radio.plan.channels] if self.radio else [],
            "power_ok": self.mode == "bridge" and self.allow_power,
            "frames": len(self.frames),
            "event": self.last_event,
        }
        return s

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=50)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        if q in self.subscribers:
            self.subscribers.remove(q)

    def notify(self):
        for q in list(self.subscribers):
            try:
                q.put_nowait(1)
            except queue.Full:
                pass


# ---------------------------------------------------------------------------
# Web panel
# ---------------------------------------------------------------------------

class PanelServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, head: VirtualHead, token: str | None = None):
        self.head = head
        self.token = token
        with open(os.path.join(HERE, "vch_ui.html"), "rb") as f:
            self.page = f.read()
        super().__init__(addr, PanelHandler)

    def stop(self):
        self.shutdown()


class PanelHandler(http.server.BaseHTTPRequestHandler):
    server: PanelServer

    def log_message(self, fmt, *a):
        log.debug("http: " + fmt, *a)

    def _auth(self) -> bool:
        tok = self.server.token
        if not tok:
            return True
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("token", [""])[0] == tok or self.headers.get("X-Token") == tok:
            return True
        self._json(403, {"error": "token required"})
        return False

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(self.server.page)))
            self.end_headers()
            self.wfile.write(self.server.page)
            return
        if not self._auth():
            return
        head = self.server.head
        if path == "/api/state":
            self._json(200, head.state())
        elif path == "/api/frames":
            with head.lock:
                fr = list(head.frames)[-200:]
            self._json(200, [dataclasses.asdict(f) | {"data": f.hex} for f in fr])
        elif path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = head.subscribe()
            try:
                last = None
                while True:
                    try:
                        q.get(timeout=0.5)
                    except queue.Empty:
                        pass
                    s = json.dumps(head.state())
                    if s != last:
                        self.wfile.write(b"data: " + s.encode() + b"\n\n")
                        self.wfile.flush()
                        last = s
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                head.unsubscribe(q)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        head = self.server.head
        if path == "/api/key":
            ok, msg = head.press(str(body.get("key", "")))
        elif path == "/api/channel":
            ok, msg = head.select_channel(int(body.get("n", 0)))
        else:
            self._json(404, {"error": "not found"})
            return
        self._json(200 if ok else 409, {"ok": ok, "msg": msg})
        head.notify()


def start_from_config(cfg: dict, radio, backend) -> list:
    """Called by tkx90-catd when [vch] enabled = true."""
    vcfg = cfg.get("vch", {})
    mode = vcfg.get("mode", "dbonly")
    cb = Codebook(vcfg.get("codebook", "/etc/tkx90/vch-codebook.json"))
    hb = None
    parts = []
    if mode == "tap":
        hb = backend
        if hasattr(backend, "listeners"):
            orig = backend.on_reconnect

            def again(b, _orig=orig):
                if _orig:
                    _orig(b)
                try:
                    b.command("HB TAP")
                except Exception as e:
                    log.warning("HB TAP: %s", e)
            backend.on_reconnect = again
    elif mode == "bridge":
        import tkx90_catd as cd
        hb = cd.PicoBackend(vcfg.get("bridge_device", "/dev/tkx90vch"), poll_cmd=None)
        sub = "HEADLESS" if vcfg.get("headless", False) else "BRIDGE"

        def setup(b):
            try:
                b.command("AR CLR")
                for r in cb.auto:
                    b.command(f"AR ADD {r['match']} {r['reply']}")
                b.command(f"HB {sub}")
            except Exception as e:
                log.warning("bridge setup: %s", e)
        hb.on_reconnect = setup
        hb.start()
        parts.append(hb)
    head = VirtualHead(mode, cb, radio=radio, ctl=backend, hb_link=hb,
                       allow_power=bool(vcfg.get("allow_power", False)))
    addr = (vcfg.get("listen", "127.0.0.1"), int(vcfg.get("port", 8073)))
    srv = PanelServer(addr, head, vcfg.get("token") or None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("virtual control head (%s) on http://%s:%d/", mode, *addr)
    parts.append(srv)
    return parts


# ---------------------------------------------------------------------------
# Command-line learning tools
# ---------------------------------------------------------------------------

class Collector:
    def __init__(self):
        self.frames: list[Frame] = []
        self.lock = threading.Lock()

    def __call__(self, line):
        f = Frame.parse(line)
        if f:
            with self.lock:
                self.frames.append(f)

    def take(self) -> list[Frame]:
        with self.lock:
            out, self.frames = self.frames, []
        return out


def open_link(args):
    sys.path.insert(0, HERE)
    import tkx90_catd as cd
    link = cd.PicoBackend(args.device, poll_cmd=None)
    col = Collector()
    link.listeners.append(col)
    mode_cmd = "HB TAP" if args.tap else ("HB BRIDGE" if not args.headless else "HB HEADLESS")
    ready = threading.Event()

    def setup(b):
        try:
            print("controller:", b.command("ID"))
            print(b.command(mode_cmd))
        except Exception as e:
            print("setup failed:", e)
        ready.set()
    link.on_reconnect = setup
    link.start()
    if not ready.wait(10):
        raise SystemExit(f"no controller on {args.device}")
    return link, col


def cli(argv=None):
    ap = argparse.ArgumentParser(description="TK-x90 virtual control head tools")
    ap.add_argument("-d", "--device", default="/dev/tkx90ctl")
    ap.add_argument("-c", "--codebook", default="vch-codebook.json")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--tap", action="store_true", help="controller J3 tap (default)")
    g.add_argument("--bridge", action="store_true", help="inline bridge board")
    ap.add_argument("--headless", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("capture"); p.add_argument("-s", type=float, default=30); p.add_argument("-o", default="-")
    p = sub.add_parser("learn-baseline"); p.add_argument("-s", type=float, default=20)
    p = sub.add_parser("learn-key"); p.add_argument("key"); p.add_argument("-s", type=float, default=4)
    p = sub.add_parser("learn-display"); p.add_argument("text"); p.add_argument("--field", default="main"); p.add_argument("-s", type=float, default=5)
    p = sub.add_parser("learn-auto"); p.add_argument("-s", type=float, default=60)
    p = sub.add_parser("press"); p.add_argument("key")
    sub.add_parser("show")
    args = ap.parse_args(argv)
    if not args.bridge:
        args.tap = True
    logging.basicConfig(level=logging.WARNING)
    cb = Codebook(args.codebook)
    cb.path = args.codebook

    if args.cmd == "show":
        print(f"keys:    {', '.join(sorted(cb.keys)) or '-'}")
        print(f"display: {len(cb.display)} rule(s)")
        for r in cb.display:
            print(f"   {r.field}: prefix {r.prefix} offset {r.offset} len {r.length}")
        print(f"auto:    {len(cb.auto)} response(s)")
        print(f"ignore:  {len(cb.ignore)} baseline frame(s)")
        missing = [k for k in KCH11_KEYS if k not in cb.keys]
        if missing:
            print(f"not yet learned: {', '.join(missing)}")
        return 0

    link, col = open_link(args)
    try:
        if args.cmd == "capture":
            out = sys.stdout if args.o == "-" else open(args.o, "w")
            end = time.time() + args.s
            while time.time() < end:
                time.sleep(0.2)
                for f in col.take():
                    out.write(json.dumps(dataclasses.asdict(f) | {"data": f.hex}) + "\n")
            out.flush()
        elif args.cmd == "learn-baseline":
            print(f"Leave the radio alone for {args.s:.0f} s ...")
            col.take(); time.sleep(args.s)
            fr = col.take()
            cb.ignore |= {f.hex for f in fr if f.kind == "rx"}
            print(f"{len(fr)} frames, {len(cb.ignore)} distinct baseline frames")
            cb.save()
        elif args.cmd == "learn-key":
            col.take()
            input(f"Press ENTER, then press and release {args.key} on the real head within {args.s:.0f} s ")
            time.sleep(args.s)
            seq = learn_key_sequence(col.take(), cb.ignore, args.bridge)
            if not seq:
                print("No new frames seen.  Run learn-baseline first, or try again.")
                return 1
            for item in seq:
                print(f"  +{item['dt']:5d} ms  {item['hex']}")
            if input("Save? [Y/n] ").strip().lower() in ("", "y", "yes"):
                cb.keys[args.key.upper()] = seq
                cb.save()
        elif args.cmd == "learn-display":
            print(f"Make sure the head shows {args.text!r} (change channel away and back now) ...")
            col.take(); time.sleep(args.s)
            rule = find_text(col.take(), args.text, args.field,
                             ("D",) if args.bridge else ("D", "?"))
            if rule is None:
                print("Text not found as ASCII.  The KCH-10 segment display may not use")
                print("ASCII; save a capture and analyse it by hand.")
                return 1
            print(f"found: prefix {rule.prefix} offset {rule.offset} length {rule.length}")
            cb.display = [r for r in cb.display if r.field != rule.field] + [rule]
            cb.save()
        elif args.cmd == "learn-auto":
            if not args.bridge:
                raise SystemExit("learn-auto needs --bridge with the real head attached")
            print(f"Capturing radio/head exchanges for {args.s:.0f} s; use the radio normally ...")
            col.take(); time.sleep(args.s)
            pairs = learn_auto_pairs(col.take())
            for r in pairs[:20]:
                print(f"  {r['match']} -> {r['reply']}  ({r['seen']}x)")
            cb.auto = [{"match": r["match"], "reply": r["reply"]} for r in pairs]
            cb.save()
            print(f"{len(cb.auto)} auto-responses saved")
        elif args.cmd == "press":
            seq = cb.keys.get(args.key.upper())
            if not seq:
                raise SystemExit("not learned")
            t0 = time.monotonic()
            for item in seq:
                d = item["dt"] / 1000 - (time.monotonic() - t0)
                if d > 0:
                    time.sleep(d)
                print(link.command(f"HBTX D {item['hex']}"))
            time.sleep(0.5)
            for f in col.take():
                print(f"  {f.kind} {f.src} {f.hex}")
    finally:
        link.stop()
    return 0


if __name__ == "__main__":
    sys.exit(cli())
