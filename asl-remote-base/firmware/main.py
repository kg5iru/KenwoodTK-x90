# TK-x90 CAT/IO controller firmware
#
# MicroPython for the Raspberry Pi Pico (RP2040).  Copy this file to the
# Pico as main.py (e.g. `mpremote cp main.py :main.py`).
#
# The Pico sits on the TK-x90 interface board and drives the radio's
# DB-25 accessory port:
#
#   * AI2..AI5 (CH Select A..D)  -> selects Remote Channel 1..15
#   * AI1      (External PTT)    -> gated copy of the URI's PTT output
#   * MCM / SPM                  -> mic mute / speaker mute
#   * SQ, TXS, AO1..AO4          -> status inputs
#   * RSI                        -> RSSI (ADC)
#   * TXD2/RXD2                  -> second serial port (research bridge)
#   * TRD (12-pin head bus)      -> virtual control head, tap mode
#                                   (headbus.py, optional J3)
#
# The host talks to it over USB CDC with a line-oriented ASCII protocol.
# See ../firmware/PROTOCOL.md.
#
# Licensed CC-BY-SA 4.0, same as the rest of this repository.

import sys
import time

try:
    import ujson as json
except ImportError:  # CPython test harness
    import json

try:
    import ubinascii as binascii
except ImportError:
    import binascii

from machine import Pin, ADC, UART

VERSION = "1.0"
CFG_FILE = "cfg.json"

DEFAULTS = {
    # XOR mask applied to the 4 channel-select bits before driving the
    # MOSFETs.  0 => a '1' bit pulls the radio line LOW (active).
    "ch_invert": 0,
    # Hold-off after a channel change before PTT/COS are allowed (ms).
    "settle_ms": 250,
    # Transmit time-out timer in seconds (0 disables).  Only effective
    # when PTT is routed through the Pico (JP1 = "PICO").
    "tot_s": 180,
    # Channel to select at power-up (0 = release lines, radio uses the
    # channel chosen on the front panel).
    "boot_ch": 0,
    # Suppress COS towards the URI while switching channels.
    "cos_gate": 1,
    # Suppress COS while the radio is transmitting.
    "cos_gate_tx": 1,
    # Baud rate for the TXD2/RXD2 bridge.
    "u2_baud": 9600,
    # Head bus (TRD): baud rate and inter-frame gap in ms.
    "trd_baud": 9600,
    "hb_gap_ms": 4,
    # Mute the front-panel microphone (MCM) whenever the Pico keys the
    # radio, so cab/shack noise does not ride along on remote-base TX.
    "mm_on_tx": 1,
    # Hardware watchdog (ms).  0 disables.  Enable it (e.g. 4000) once
    # commissioned: the REPL/mpremote cannot be used while it runs, so
    # firmware updates then need the BOOTSEL button.
    "wdt_ms": 0,
}

# ---------------------------------------------------------------------------
# Pin map (see ../hardware/INTERFACE.md)
# ---------------------------------------------------------------------------
PIN_U2_TX = 0      # UART0 TX  -> level shifter -> DB25-10 RXD2
PIN_U2_RX = 1      # UART0 RX  <- level shifter <- DB25-9  TXD2
PIN_CH = (2, 3, 4, 5)  # -> Q2..Q5 -> DB25-3..6  AI2..AI5 (CH Select A..D)
PIN_PTT_OUT = 6    # -> Q1 -> DB25-2 AI1 (External PTT)
PIN_PTT_IN = 7     # <- URI PTT (open collector, active low)
PIN_MCM = 8        # -> Q6 -> DB25-12 MCM (mic mute, low = mute)
PIN_SPM = 9        # -> Q7 -> DB25-24 SPM (speaker mute, low = mute)
PIN_SQ = 10        # <- D  <- DB25-23 SQ  (low = busy)
PIN_TXS = 11       # <- D  <- DB25-11 TXS (high = TX)
PIN_AO = (12, 13, 14, 15)  # <- D <- DB25-20,8,21,22 AO1..AO4
PIN_COS_OUT = 16   # -> Q8 -> URI COS (open drain, active low)
PIN_TRD_RX = 17    # <- R7/R8 divider <- J3 TRD (12-pin connector pin 6)
PIN_TRD_TX = 20    # UART1 TX, inverted -> Q10 open drain -> J3 TRD
PIN_LED = 25
ADC_RSSI = 0       # GP26 <- DB25-1 RSI


class Controller:
    """All controller logic.  Hardware access is limited to the Pin/ADC/
    UART objects created in __init__, so the class can be exercised on a
    PC with a stub `machine` module."""

    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.cfg = dict(DEFAULTS)
        self._load_cfg()

        self.ch_pins = [Pin(n, Pin.OUT, value=0) for n in PIN_CH]
        self.ptt_out = Pin(PIN_PTT_OUT, Pin.OUT, value=0)
        self.mcm = Pin(PIN_MCM, Pin.OUT, value=0)
        self.spm = Pin(PIN_SPM, Pin.OUT, value=0)
        self.cos_out = Pin(PIN_COS_OUT, Pin.OUT, value=0)
        self.led = Pin(PIN_LED, Pin.OUT, value=0)

        self.ptt_in = Pin(PIN_PTT_IN, Pin.IN, Pin.PULL_UP)
        self.sq = Pin(PIN_SQ, Pin.IN, Pin.PULL_UP)
        self.txs = Pin(PIN_TXS, Pin.IN, Pin.PULL_UP)
        self.ao = [Pin(n, Pin.IN, Pin.PULL_UP) for n in PIN_AO]
        self.adc = ADC(ADC_RSSI)

        self.u2 = None
        self.u2_mon = False
        self.trd = None
        self.hb = None

        self.channel = 0
        self.settle_until = 0
        self.ptt_active = False
        self.ptt_start = 0
        self.tot_tripped = False
        self.cos_active = False
        self.rx_buf = ""
        self.last_led = 0
        self.mic_mute = False

        self.set_channel(int(self.cfg["boot_ch"]))

    # -- configuration -------------------------------------------------------
    def _load_cfg(self):
        try:
            with open(CFG_FILE) as f:
                data = json.load(f)
            for k in DEFAULTS:
                if k in data:
                    self.cfg[k] = int(data[k])
        except (OSError, ValueError):
            pass

    def _save_cfg(self):
        try:
            with open(CFG_FILE, "w") as f:
                json.dump(self.cfg, f)
            return True
        except OSError:
            return False

    # -- I/O helpers -----------------------------------------------------------
    def reply(self, line):
        self.out.write(line + "\r\n")

    def now(self):
        return time.ticks_ms()

    def since(self, t):
        return time.ticks_diff(self.now(), t)

    def is_tx(self):
        return self.txs.value() == 1

    def ptt_requested(self):
        return self.ptt_in.value() == 0

    def sq_busy(self):
        return self.sq.value() == 0

    def rssi_mv(self):
        # 16-bit reading scaled to 3.3 V reference.
        return (self.adc.read_u16() * 3300) // 65535

    def settling(self):
        return time.ticks_diff(self.settle_until, self.now()) > 0

    # -- actions -----------------------------------------------------------------
    def set_channel(self, ch):
        bits = (ch & 0x0F) ^ (self.cfg["ch_invert"] & 0x0F)
        # Drop PTT/COS before touching the select lines.
        self.ptt_out.value(0)
        self.cos_out.value(0)
        for i, p in enumerate(self.ch_pins):
            p.value((bits >> i) & 1)
        self.channel = ch
        self.settle_until = time.ticks_add(self.now(), self.cfg["settle_ms"])

    def u2_open(self):
        self.u2 = UART(0, baudrate=self.cfg["u2_baud"],
                       tx=Pin(PIN_U2_TX), rx=Pin(PIN_U2_RX))

    def trd_open(self):
        import headbus
        # Inverted TX: UART idle (mark) = gate low = TRD released.
        self.trd = UART(1, baudrate=self.cfg["trd_baud"],
                        tx=Pin(PIN_TRD_TX), rx=Pin(PIN_TRD_RX),
                        invert=UART.INV_TX)
        self.hb = headbus.Tap(self.trd, gap_ms=self.cfg["hb_gap_ms"])

    # -- periodic work -------------------------------------------------------------
    def tick(self):
        # PTT gate: URI request -> AI1, unless settling or timed out.
        req = self.ptt_requested()
        if req and not self.ptt_active:
            self.ptt_start = self.now()
        if not req:
            self.tot_tripped = False
        tot = self.cfg["tot_s"]
        if req and tot and self.since(self.ptt_start) > tot * 1000:
            if not self.tot_tripped:
                self.reply("EV TOT")
            self.tot_tripped = True
        self.ptt_active = req
        key = req and not self.tot_tripped and not self.settling()
        self.ptt_out.value(1 if key else 0)
        if self.cfg["mm_on_tx"]:
            self.mcm.value(1 if (key or self.mic_mute) else 0)

        # COS towards the URI.
        cos = self.sq_busy()
        if self.cfg["cos_gate"] and self.settling():
            cos = False
        if self.cfg["cos_gate_tx"] and self.is_tx():
            cos = False
        self.cos_out.value(1 if cos else 0)
        self.cos_active = cos

        # Research bridges: forward anything received as hex.
        if self.u2 is not None and self.u2_mon and self.u2.any():
            data = self.u2.read()
            if data:
                self.reply("U2R " + binascii.hexlify(data).decode())
        if self.hb is not None:
            for kind, src, frame in self.hb.service(self.now()):
                self.reply("HB %s %s %d %s" % (
                    kind, src, self.now() & 0xFFFFFF,
                    binascii.hexlify(frame).decode()))

        # Heartbeat LED: slow blink idle, solid while transmitting.
        if key:
            self.led.value(1)
        elif self.since(self.last_led) > 500:
            self.led.toggle()
            self.last_led = self.now()

    def feed(self, chars):
        for c in chars:
            if c in "\r\n":
                line = self.rx_buf.strip()
                self.rx_buf = ""
                if line:
                    self.command(line)
            elif len(self.rx_buf) < 256:
                self.rx_buf += c

    # -- command interpreter -----------------------------------------------------------
    def status(self):
        ao = "".join("1" if p.value() == 0 else "0" for p in self.ao)
        return "ST ch=%d sq=%d tx=%d ptt=%d cos=%d tot=%d ao=%s rssi=%d mm=%d sm=%d" % (
            self.channel,
            1 if self.sq_busy() else 0,
            1 if self.is_tx() else 0,
            1 if self.ptt_requested() else 0,
            1 if self.cos_active else 0,
            1 if self.tot_tripped else 0,
            ao,
            self.rssi_mv(),
            self.mcm.value(),
            self.spm.value(),
        )

    def command(self, line):
        parts = line.split()
        cmd = parts[0].upper()
        args = parts[1:]
        try:
            if cmd == "ID":
                self.reply("ID TKX90CTL " + VERSION)
            elif cmd in ("CH?", "CH") and not args:
                self.reply("CH %d" % self.channel)
            elif cmd == "CH":
                ch = int(args[0])
                if ch < 0 or ch > 15:
                    self.reply("ERR RANGE")
                elif self.is_tx() or self.ptt_out.value():
                    self.reply("ERR TX")
                else:
                    self.set_channel(ch)
                    self.reply("CH %d" % ch)
            elif cmd in ("ST?", "ST"):
                self.reply(self.status())
            elif cmd == "MM":
                self.mic_mute = bool(int(args[0]))
                self.mcm.value(1 if self.mic_mute else 0)
                self.reply("MM %d" % self.mcm.value())
            elif cmd == "SM":
                self.spm.value(1 if int(args[0]) else 0)
                self.reply("SM %d" % self.spm.value())
            elif cmd == "CFG?" or (cmd == "CFG" and not args):
                self.reply("CFG " + " ".join(
                    "%s=%d" % (k, self.cfg[k]) for k in sorted(self.cfg)))
            elif cmd == "CFG":
                for a in args:
                    k, v = a.split("=", 1)
                    if k not in DEFAULTS:
                        raise ValueError(k)
                    self.cfg[k] = int(v)
                self.reply("CFG OK")
            elif cmd == "SAVE":
                self.reply("SAVE OK" if self._save_cfg() else "ERR FS")
            elif cmd == "U2":
                # U2 ON | OFF  (monitor), U2 BAUD n
                if args[0].upper() == "ON":
                    if self.u2 is None:
                        self.u2_open()
                    self.u2_mon = True
                elif args[0].upper() == "OFF":
                    self.u2_mon = False
                elif args[0].upper() == "BAUD":
                    self.cfg["u2_baud"] = int(args[1])
                    self.u2_open()
                self.reply("U2 OK")
            elif cmd == "U2W":
                if self.u2 is None:
                    self.u2_open()
                self.u2.write(binascii.unhexlify(args[0]))
                self.reply("U2W OK")
            elif cmd in ("HB", "TRD"):
                # HB TAP | HB OFF   (TRD ON/OFF kept as aliases)
                if args[0].upper() in ("TAP", "ON"):
                    self.trd_open()
                    self.reply("HB TAP")
                else:
                    if self.trd is not None:
                        self.trd.deinit()
                    self.trd = None
                    self.hb = None
                    self.reply("HB OFF")
            elif cmd == "HBTX":
                # HBTX <D|H> <hex>  (direction is ignored in tap mode)
                if self.hb is None:
                    self.reply("ERR HB")
                else:
                    self.hb.send(args[0].upper(), binascii.unhexlify(args[1]))
                    self.reply("HBTX OK")
            elif cmd == "RESET":
                self.reply("RESET OK")
                import machine
                machine.reset()
            else:
                self.reply("ERR CMD")
        except (IndexError, ValueError):
            self.reply("ERR ARG")


def main():
    import select
    from machine import WDT

    ctl = Controller()
    wdt = WDT(timeout=ctl.cfg["wdt_ms"]) if ctl.cfg["wdt_ms"] else None
    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    ctl.reply("ID TKX90CTL " + VERSION)
    while True:
        while poll.poll(0):
            ctl.feed(sys.stdin.read(1))
        ctl.tick()
        if wdt:
            wdt.feed()
        time.sleep_ms(1)


if __name__ == "__main__":
    main()
