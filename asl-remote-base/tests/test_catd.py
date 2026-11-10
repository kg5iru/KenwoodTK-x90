"""Tests for tkx90_catd.  Run:  python3 -m unittest discover -s asl-remote-base/tests"""

import os
import socket
import sys
import threading
import time
import tty
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "host"))

import tkx90_catd as cd  # noqa: E402

CFG_PATH = os.path.join(HERE, "..", "host", "tkx90.toml")


def app_rpt_vw(freq, offset_mode, txplon, rxplon, txpl, rxpl, split_khz):
    """Re-implementation of app_rpt's setkenwood() VW formatting
    (apps/app_rpt/rpt_rig.c), including its split_freq() truncation."""
    pl = {v: k for k, v in cd.KENWOOD_TONES.items()}

    def code(s):
        return pl.get(float(s), -1)

    mhz, dec = freq.split(".")
    decimals = (dec + "00000")[:3]          # split_freq keeps 3 digits
    fr = decimals[:4]
    offsets = {"simplex": 0, "minus": 2, "plus": 1}
    band = "6" if int(mhz) > 400 else "2"
    return "VW %s,%05d%s,%d,%d,0,%d,%d,,%02d,,%02d,%s" % (
        band, int(mhz), fr, 1, offsets[offset_mode], txplon, rxplon,
        code(txpl), code(rxpl), "%06d000" % split_khz)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cd.load_config(CFG_PATH)
        self.plan = cd.plan_from_config(self.cfg)
        self.backend = cd.SimBackend()
        self.cfg["daemon"]["status_file"] = None
        self.radio = cd.Radio(self.plan, self.backend, self.cfg)

    def test_plan_loads(self):
        self.assertEqual(len(self.plan.channels), 6)
        ch = self.plan.by_number(2)
        self.assertEqual(ch.tx_hz, 146_340_000)
        self.assertEqual(ch.shift, cd.SHIFT_MINUS)

    def test_vw_parse_matches_app_rpt_format(self):
        line = app_rpt_vw("147.03000", "plus", 1, 0, "103.5", "103.5", 600)
        cmd, _, args = line.partition(" ")
        req = cd.parse_vw(args)
        self.assertEqual(req.rx_hz, 147_030_000)
        self.assertEqual(req.shift, cd.SHIFT_PLUS)
        self.assertEqual(req.offset_hz, 600_000)
        self.assertEqual(req.tx_tone, 103.5)
        self.assertIsNone(req.rx_tone)

    def test_kenwood_emulator_selects_channel(self):
        emu = cd.KenwoodEmulator(self.radio)
        self.assertEqual(emu.handle("VMC 0,0"), "VMC 0,0")
        line = app_rpt_vw("146.94000", "minus", 1, 0, "100.0", "100.0", 600)
        self.assertEqual(emu.handle(line), line)
        self.assertEqual(self.backend.status.channel, 2)
        self.assertEqual(emu.handle("RBN 2"), "RBN 2")
        self.assertEqual(emu.handle("PC 0,1"), "PC 0,1")

    def test_tone_squelch_preference(self):
        emu = cd.KenwoodEmulator(self.radio)
        emu.handle(app_rpt_vw("147.03000", "plus", 1, 1, "103.5", "103.5", 600))
        self.assertEqual(self.backend.status.channel, 4)
        emu.handle(app_rpt_vw("147.03000", "plus", 1, 0, "103.5", "103.5", 600))
        self.assertEqual(self.backend.status.channel, 3)

    def test_unknown_frequency_rejected(self):
        emu = cd.KenwoodEmulator(self.radio)
        self.assertEqual(emu.handle(app_rpt_vw("146.76000", "minus", 0, 0, "100.0", "100.0", 600)), "N")

    def test_strict_mode(self):
        self.radio.strict = True
        emu = cd.KenwoodEmulator(self.radio)
        # 146.94 is programmed with tone 100.0, request without tone fails
        self.assertEqual(emu.handle(app_rpt_vw("146.94000", "minus", 0, 0, "100.0", "100.0", 600)), "N")
        line = app_rpt_vw("146.94000", "minus", 1, 0, "100.0", "100.0", 600)
        self.assertEqual(emu.handle(line), line)

    def test_tx_interlock(self):
        emu = cd.KenwoodEmulator(self.radio)
        self.backend.status.tx = True
        self.assertEqual(emu.handle(app_rpt_vw("146.52000", "simplex", 0, 0, "100.0", "100.0", 600)), "N")

    def test_asl_memory(self):
        txt = cd.asl_memory_stanza(self.plan)
        self.assertIn("02 = 146.940,100.0,h-t", txt)
        self.assertIn("04 = 147.030,103.5,h+tr", txt)

    def test_rigctl_session(self):
        s = cd.RigctlSession(self.radio, self.cfg)
        self.assertEqual(s.execute("\\chk_vfo")[0], ["0"])
        st = s.execute("\\dump_state")[0]
        self.assertEqual(st[0], "1")
        self.assertEqual(st[-1], "done")
        self.assertEqual(s.execute("F 147030000")[0], ["RPRT 0"])
        self.assertEqual(s.execute("f")[0], ["147030000"])
        self.assertEqual(s.execute("r")[0], ["+"])
        self.assertEqual(s.execute("c")[0], ["1035"])
        # turn on tone squelch -> channel 4
        self.assertEqual(s.execute("\\set_ctcss_sql 1035")[0], ["RPRT 0"])
        self.assertEqual(s.execute("e")[0], ["4"])
        self.assertEqual(s.execute("F 146760000")[0], ["RPRT -1"])
        self.assertEqual(s.execute("T 1")[0], ["RPRT -11"])
        self.assertEqual(s.execute("E 1")[0], ["RPRT 0"])
        self.assertEqual(s.execute("f")[0], ["146520000"])
        self.assertEqual(s.execute("m")[0], ["FM", "15000"])
        out = s.execute("+f")[0]
        self.assertEqual(out[-1], "RPRT 0")

    def test_rssi(self):
        self.assertAlmostEqual(self.radio.rssi_dbm(300), -120.0)
        self.assertAlmostEqual(self.radio.rssi_dbm(1450), -90.0)
        self.assertAlmostEqual(self.radio.rssi_dbm(500), -115.0)


class PtyEndToEnd(unittest.TestCase):
    """Drive the pty exactly the way app_rpt does (byte-at-a-time, CR)."""

    def test_pty(self):
        cfg = cd.load_config(CFG_PATH)
        cfg["daemon"]["status_file"] = None
        plan = cd.plan_from_config(cfg)
        backend = cd.SimBackend()
        radio = cd.Radio(plan, backend, cfg)
        link = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"tkx90-test-{os.getpid()}")
        pty = cd.KenwoodPty(cd.KenwoodEmulator(radio), link, group=None)
        pty.start()
        try:
            fd = os.open(link, os.O_RDWR | os.O_NOCTTY)
            tty.setraw(fd)

            def xfer(cmd):
                for ch in (cmd + "\r").encode():
                    os.write(fd, bytes([ch]))
                    time.sleep(0.002)
                buf = b""
                deadline = time.time() + 2
                while not buf.endswith(b"\r") and time.time() < deadline:
                    buf += os.read(fd, 64)
                return buf.decode().strip()

            self.assertEqual(xfer("VMC 0,0"), "VMC 0,0")
            line = app_rpt_vw("146.94000", "minus", 1, 0, "100.0", "100.0", 600)
            self.assertEqual(xfer(line), line)
            self.assertEqual(backend.status.channel, 2)
            os.close(fd)
        finally:
            pty.stop()


class RigctlTcp(unittest.TestCase):
    def test_tcp(self):
        cfg = cd.load_config(CFG_PATH)
        cfg["daemon"]["status_file"] = None
        radio = cd.Radio(cd.plan_from_config(cfg), cd.SimBackend(), cfg)
        srv = cd.RigctlServer(("127.0.0.1", 0), radio, cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            s = socket.create_connection(srv.server_address)
            f = s.makefile("rw")
            f.write("F 146520000\n"); f.flush()
            self.assertEqual(f.readline().strip(), "RPRT 0")
            f.write("f\n"); f.flush()
            self.assertEqual(f.readline().strip(), "146520000")
            f.write("q\n"); f.flush()
            s.close()
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
