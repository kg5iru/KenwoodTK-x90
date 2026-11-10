"""Run the Pico firmware's Controller on CPython with a stub `machine` module."""

import io
import os
import sys
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FW = os.path.join(HERE, "..", "firmware")


class FakePin:
    OUT, IN, PULL_UP = 1, 0, 2
    pins = {}

    def __init__(self, n, mode=0, pull=None, value=None):
        self.n = n
        self.mode = mode
        self.v = 1 if (mode == self.IN and pull == self.PULL_UP) else (value or 0)
        FakePin.pins[n] = self

    def value(self, v=None):
        if v is None:
            return self.v
        self.v = 1 if v else 0

    def toggle(self):
        self.v ^= 1


class FakeADC:
    def __init__(self, ch):
        self.raw = 0

    def read_u16(self):
        return self.raw


class FakeUART:
    def __init__(self, *a, **k):
        self.tx = b""

    def any(self):
        return 0

    def write(self, b):
        self.tx += b

    def deinit(self):
        pass


class Clock:
    t = 0


def install_stubs():
    m = types.ModuleType("machine")
    m.Pin, m.ADC, m.UART = FakePin, FakeADC, FakeUART
    sys.modules["machine"] = m
    time.ticks_ms = lambda: Clock.t
    time.ticks_diff = lambda a, b: a - b
    time.ticks_add = lambda a, b: a + b


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        install_stubs()
        sys.path.insert(0, FW)
        cwd = os.getcwd()
        os.chdir(os.environ.get("TMPDIR", "/tmp"))
        self.addCleanup(os.chdir, cwd)
        sys.modules.pop("main", None)
        import main  # noqa
        self.fw = main
        FakePin.pins.clear()
        Clock.t = 0
        self.out = io.StringIO()
        self.ctl = main.Controller(out=self.out)
        # Idle radio: TXS is driven low (receive), SQ high (not busy).
        self.pin(11).value(0)

    def cmd(self, line):
        self.out.seek(0)
        self.out.truncate()
        self.ctl.feed(line + "\r")
        return self.out.getvalue().strip()

    def pin(self, n):
        return FakePin.pins[n]

    def test_id(self):
        self.assertTrue(self.cmd("ID").startswith("ID TKX90CTL"))

    def test_channel_bits(self):
        self.assertEqual(self.cmd("CH 5"), "CH 5")
        self.assertEqual([self.pin(n).value() for n in (2, 3, 4, 5)], [1, 0, 1, 0])
        self.assertEqual(self.cmd("CH"), "CH 5")
        self.assertEqual(self.cmd("CH 16"), "ERR RANGE")

    def test_invert(self):
        self.cmd("CFG ch_invert=15")
        self.cmd("CH 1")
        self.assertEqual([self.pin(n).value() for n in (2, 3, 4, 5)], [0, 1, 1, 1])

    def test_refuse_change_while_tx(self):
        self.pin(11).value(1)   # TXS high
        self.assertEqual(self.cmd("CH 2"), "ERR TX")

    def test_ptt_gating_and_settle(self):
        self.cmd("CH 3")
        self.pin(7).value(0)    # URI asks to transmit
        self.ctl.tick()
        self.assertEqual(self.pin(6).value(), 0, "PTT must wait for settle")
        Clock.t = 300
        self.ctl.tick()
        self.assertEqual(self.pin(6).value(), 1)
        self.pin(7).value(1)
        self.ctl.tick()
        self.assertEqual(self.pin(6).value(), 0)

    def test_tot(self):
        self.cmd("CFG tot_s=1")
        Clock.t = 1000
        self.pin(7).value(0)
        self.ctl.tick()
        self.assertEqual(self.pin(6).value(), 1)
        Clock.t = 2500
        self.ctl.tick()
        self.assertEqual(self.pin(6).value(), 0)
        self.assertIn("EV TOT", self.out.getvalue())
        self.pin(7).value(1)
        self.ctl.tick()
        self.pin(7).value(0)
        Clock.t = 2600
        self.ctl.tick()
        self.assertEqual(self.pin(6).value(), 1)

    def test_cos(self):
        Clock.t = 1000
        self.pin(10).value(0)   # SQ low = busy
        self.ctl.tick()
        self.assertEqual(self.pin(16).value(), 1)
        self.cmd("CH 2")        # settling suppresses COS
        self.ctl.tick()
        self.assertEqual(self.pin(16).value(), 0)
        self.pin(10).value(1)

    def test_status(self):
        st = self.cmd("ST")
        self.assertTrue(st.startswith("ST ch=0 sq=0 tx=0 ptt=0"))

    def test_u2_write(self):
        self.assertEqual(self.cmd("U2W 0102ff"), "U2W OK")
        self.assertEqual(self.ctl.u2.tx, b"\x01\x02\xff")

    def test_bad(self):
        self.assertEqual(self.cmd("FOO"), "ERR CMD")
        self.assertEqual(self.cmd("CH x"), "ERR ARG")


if __name__ == "__main__":
    unittest.main()
