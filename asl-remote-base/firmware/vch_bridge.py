# TK-x90 virtual control head -- inline bridge firmware
#
# MicroPython for a second Raspberry Pi Pico on the VCH bridge board,
# which sits on the W501 head cable between the radio and the KCH-10/11
# (or replaces the head entirely).  Copy as main.py together with
# headbus.py:
#
#   mpremote cp headbus.py :headbus.py
#   mpremote cp vch_bridge.py :main.py
#
# See ../hardware/VIRTUAL_HEAD.md for the board and ../host/vch.py for
# the host side.  Line protocol: ../firmware/PROTOCOL.md ("VCH bridge").
#
# Licensed CC-BY-SA 4.0, same as the rest of this repository.

import sys
import time

try:
    import ubinascii as binascii
except ImportError:
    import binascii

from machine import Pin, UART

import headbus

VERSION = "1.0"

PIN_DECK_TX = 0    # UART0 TX (inverted) -> Q1 -> TRD, radio side
PIN_DECK_RX = 1    # UART0 RX <- divider <- TRD, radio side
PIN_HEAD_TX = 4    # UART1 TX (inverted) -> Q2 -> TRD, head side
PIN_HEAD_RX = 5    # UART1 RX <- divider <- TRD, head side
PIN_PS_OUT = 6     # -> Q3 -> PS (pull to E = press POWER)
PIN_PS_IN = 7      # <- D1 <- PS   (low = someone is pressing POWER)
PIN_RST_IN = 8     # <- D2 <- RST  (radio resetting the head)
PIN_SB_IN = 9      # <- 100k/27k divider <- SB (radio switched on)
PIN_RELAY = 10     # -> Q4 -> K1 coil: energised = TRD split through Pico
PIN_LED = 25

MODES = ("BYPASS", "BRIDGE", "HEADLESS")


class BridgeController:
    def __init__(self, out=None, baud=9600, gap_ms=4):
        self.out = out or sys.stdout
        self.baud = baud
        self.gap_ms = gap_ms
        self.deck_uart = UART(0, baudrate=baud, tx=Pin(PIN_DECK_TX),
                              rx=Pin(PIN_DECK_RX), invert=UART.INV_TX)
        self.head_uart = UART(1, baudrate=baud, tx=Pin(PIN_HEAD_TX),
                              rx=Pin(PIN_HEAD_RX), invert=UART.INV_TX)
        self.bridge = headbus.Bridge(self.deck_uart, self.head_uart, gap_ms)
        self.ps_out = Pin(PIN_PS_OUT, Pin.OUT, value=0)
        self.ps_in = Pin(PIN_PS_IN, Pin.IN, Pin.PULL_UP)
        self.rst_in = Pin(PIN_RST_IN, Pin.IN, Pin.PULL_UP)
        self.sb_in = Pin(PIN_SB_IN, Pin.IN)
        self.relay = Pin(PIN_RELAY, Pin.OUT, value=0)
        self.led = Pin(PIN_LED, Pin.OUT, value=0)
        self.mode = "BYPASS"
        self.ps_until = 0
        self.last_rst = 1
        self.last_sb = None
        self.rx_buf = ""

    def now(self):
        return time.ticks_ms()

    def reply(self, line):
        self.out.write(line + "\r\n")

    def set_mode(self, mode):
        self.mode = mode
        # K1 de-energised = TRD wired straight through (fail-safe).
        self.relay.value(0 if mode == "BYPASS" else 1)
        self.bridge.headless = mode == "HEADLESS"
        self.led.value(0 if mode == "BYPASS" else 1)

    def tick(self):
        now = self.now()
        if self.mode != "BYPASS":
            for kind, src, frame in self.bridge.service(now):
                self.reply("HB %s %s %d %s" % (kind, src, now & 0xFFFFFF,
                                               binascii.hexlify(frame).decode()))
        if self.ps_out.value() and time.ticks_diff(self.ps_until, now) <= 0:
            self.ps_out.value(0)
        rst = self.rst_in.value()
        if rst != self.last_rst:
            self.reply("EV RST %d" % (0 if rst else 1))
            self.last_rst = rst
        sb = self.sb_in.value()
        if sb != self.last_sb:
            self.reply("EV SB %d" % sb)
            self.last_sb = sb

    def feed(self, chars):
        for c in chars:
            if c in "\r\n":
                line = self.rx_buf.strip()
                self.rx_buf = ""
                if line:
                    self.command(line)
            elif len(self.rx_buf) < 300:
                self.rx_buf += c

    def command(self, line):
        p = line.split()
        cmd, a = p[0].upper(), p[1:]
        try:
            if cmd == "ID":
                self.reply("ID TKX90VCH " + VERSION)
            elif cmd == "HB":
                m = a[0].upper()
                if m == "OFF":
                    m = "BYPASS"
                if m not in MODES:
                    raise ValueError(m)
                self.set_mode(m)
                self.reply("HB " + m)
            elif cmd == "HBTX":
                if self.mode == "BYPASS":
                    self.reply("ERR BYPASS")
                else:
                    self.bridge.inject(a[0].upper(), binascii.unhexlify(a[1]))
                    self.reply("HBTX OK")
            elif cmd == "AR":
                sub = a[0].upper()
                if sub == "ADD":
                    self.bridge.add_auto(binascii.unhexlify(a[1]),
                                         binascii.unhexlify(a[2]))
                elif sub == "CLR":
                    self.bridge.clear_auto()
                self.reply("AR %d" % len(self.bridge.auto))
            elif cmd == "PWR":
                ms = int(a[0]) if a else 300
                self.ps_out.value(1)
                self.ps_until = time.ticks_add(self.now(), max(50, min(ms, 5000)))
                self.reply("PWR OK")
            elif cmd in ("ST", "ST?"):
                self.reply("VST mode=%s sb=%d rst=%d ps=%d coll=%d" % (
                    self.mode, self.sb_in.value(), 0 if self.rst_in.value() else 1,
                    0 if self.ps_in.value() else 1,
                    self.bridge.deck.collisions + self.bridge.head.collisions))
            else:
                self.reply("ERR CMD")
        except (IndexError, ValueError):
            self.reply("ERR ARG")


def main():
    import select
    ctl = BridgeController()
    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    ctl.reply("ID TKX90VCH " + VERSION)
    while True:
        while poll.poll(0):
            ctl.feed(sys.stdin.read(1))
        ctl.tick()
        time.sleep_ms(1)


if __name__ == "__main__":
    main()
