"""Tests for the virtual control head: firmware/headbus.py and host/vch.py."""

import json
import os
import sys
import threading
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "firmware"))
sys.path.insert(0, os.path.join(HERE, "..", "host"))

import headbus  # noqa: E402
import tkx90_catd as cd  # noqa: E402
import vch  # noqa: E402

CFG_PATH = os.path.join(HERE, "..", "host", "tkx90.toml")


class Wire:
    """A single TRD wire.  Everything written by any endpoint is heard by
    every endpoint on the wire, including the writer (echo)."""

    def __init__(self):
        self.ends = []

    def end(self):
        e = WireEnd(self)
        self.ends.append(e)
        return e

    def put(self, data):
        for e in self.ends:
            e.rx.extend(data)


class WireEnd:
    def __init__(self, wire):
        self.wire = wire
        self.rx = bytearray()

    def any(self):
        return len(self.rx)

    def read(self, n=None):
        n = len(self.rx) if n is None else n
        d = bytes(self.rx[:n])
        del self.rx[:n]
        return d

    def write(self, data):
        self.wire.put(data)


def run(obj, start, ms, step=1):
    ev = []
    for t in range(start, start + ms, step):
        ev += obj.service(t)
    return ev


class HeadbusTests(unittest.TestCase):
    def test_tap_rx_and_own_echo(self):
        w = Wire()
        radio = w.end()
        tap = headbus.Tap(w.end(), gap_ms=4)
        radio.write(b"\x10\x20\x30")
        ev = run(tap, 0, 10)
        self.assertIn(("rx", "?", b"\x10\x20\x30"), ev)
        tap.send("D", b"\xaa\x55")
        ev = run(tap, 20, 20)
        self.assertIn(("tx", "?", b"\xaa\x55"), ev)
        self.assertFalse([e for e in ev if e[0] == "rx"], "own echo must not be reported")

    def test_tap_listen_before_talk(self):
        w = Wire()
        radio = w.end()
        tap = headbus.Tap(w.end(), gap_ms=4, lbt_ms=6)
        tap.send("D", b"\x01")
        radio.write(b"\x99")
        ev = tap.service(0)
        self.assertEqual(radio.rx, bytearray(b"\x99"), "must not transmit on a busy bus")
        ev += run(tap, 1, 20)
        self.assertIn(("tx", "?", b"\x01"), ev)

    def test_tap_collision_retry(self):
        w = Wire()
        tap = headbus.Tap(w.end(), gap_ms=4, lbt_ms=0)
        tap.send("D", b"\x01\x02")
        tap.service(0)                       # transmits, echo pending
        tap.seg.uart.rx[1:2] = b"\x00"       # second byte corrupted on the wire
        ev = run(tap, 1, 40)
        kinds = [e[0] for e in ev]
        self.assertIn("coll", kinds)
        self.assertIn(("tx", "?", b"\x01\x02"), ev)

    def test_bridge_forwards_without_loops(self):
        dw, hw = Wire(), Wire()
        radio, head = dw.end(), hw.end()
        br = headbus.Bridge(dw.end(), hw.end(), gap_ms=4)
        radio.write(b"\xd1\xd2")
        ev = run(br, 0, 10)
        self.assertEqual(bytes(head.rx), b"\xd1\xd2", "deck frame reaches the head")
        self.assertIn(("rx", "D", b"\xd1\xd2"), ev)
        radio.rx.clear(); head.rx.clear()
        head.write(b"\xe1")
        ev = run(br, 20, 10)
        self.assertEqual(bytes(radio.rx), b"\xe1", "head frame reaches the radio once")
        self.assertIn(("rx", "H", b"\xe1"), ev)
        self.assertEqual(len([e for e in ev if e[0] == "rx"]), 1)

    def test_bridge_inject_to_deck(self):
        dw, hw = Wire(), Wire()
        radio, head = dw.end(), hw.end()
        br = headbus.Bridge(dw.end(), hw.end(), gap_ms=4)
        br.inject("D", b"\x42")
        ev = run(br, 0, 20)
        self.assertEqual(bytes(radio.rx), b"\x42")
        self.assertEqual(bytes(head.rx), b"", "injection toward the deck is not copied to the head")
        self.assertIn(("tx", "H", b"\x42"), ev)

    def test_bridge_headless_autoresponder(self):
        dw, hw = Wire(), Wire()
        radio = dw.end()
        head = hw.end()
        br = headbus.Bridge(dw.end(), hw.end(), gap_ms=4)
        br.headless = True
        br.add_auto(b"\x50", b"\x60\x61")
        radio.write(b"\x50\x01")
        ev = run(br, 0, 30)
        self.assertIn(("auto", "H", b"\x60\x61"), ev)
        self.assertTrue(bytes(radio.rx).endswith(b"\x60\x61"))
        self.assertEqual(bytes(head.rx), b"")


def F(kind, src, t, h):
    return vch.Frame(kind, src, t, bytes.fromhex(h))


class LearnTests(unittest.TestCase):
    def test_key_sequence(self):
        frames = [F("rx", "?", 100, "0101"), F("rx", "?", 110, "a1b2"),
                  F("rx", "?", 115, "a1b2"), F("rx", "?", 300, "a1b3")]
        seq = vch.learn_key_sequence(frames, {"0101"}, bridge=False)
        self.assertEqual(seq, [{"dt": 0, "hex": "a1b2"}, {"dt": 190, "hex": "a1b3"}])

    def test_key_sequence_bridge_uses_head_frames(self):
        frames = [F("rx", "D", 100, "dd"), F("rx", "H", 120, "ee")]
        self.assertEqual(vch.learn_key_sequence(frames, set(), bridge=True),
                         [{"dt": 0, "hex": "ee"}])

    def test_find_text(self):
        data = b"\x21\x07" + b"RPT 147.03    " + b"\x00"
        frames = [F("rx", "D", 1, "0102"), vch.Frame("rx", "D", 2, data)]
        rule = vch.find_text(frames, "RPT 147.03", "main")
        self.assertEqual((rule.prefix, rule.offset), ("2107", 2))
        cb = vch.Codebook()
        cb.display = [rule]
        self.assertEqual(cb.decode(data)["main"].strip(), "RPT 147.03")

    def test_auto_pairs(self):
        frames = [F("rx", "D", 0, "51"), F("rx", "H", 10, "61"),
                  F("rx", "D", 100, "51"), F("rx", "H", 112, "61"),
                  F("rx", "D", 200, "52"), F("rx", "H", 500, "62")]
        pairs = vch.learn_auto_pairs(frames, window_ms=60)
        self.assertEqual(pairs, [{"match": "51", "reply": "61", "seen": 2}])

    def test_codebook_roundtrip(self):
        p = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"cb-{os.getpid()}.json")
        cb = vch.Codebook(p)
        cb.keys["PF1"] = [{"dt": 0, "hex": "aa"}]
        cb.display.append(vch.DisplayRule("main", "21", 2, 14))
        cb.save()
        cb2 = vch.Codebook(p)
        self.assertEqual(cb2.keys, cb.keys)
        self.assertEqual(cb2.display[0].offset, 2)
        os.unlink(p)


class FakeLink:
    def __init__(self):
        self.listeners = []
        self.sent = []

    def command(self, line):
        self.sent.append(line)
        return "HBTX OK"


class PanelTests(unittest.TestCase):
    def setUp(self):
        cfg = cd.load_config(CFG_PATH)
        cfg["daemon"]["status_file"] = None
        self.backend = cd.SimBackend()
        self.radio = cd.Radio(cd.plan_from_config(cfg), self.backend, cfg)
        self.radio._reapply(self.backend)

    def test_level1_channel_knob(self):
        head = vch.VirtualHead("dbonly", vch.Codebook(), radio=self.radio, ctl=self.backend)
        self.assertEqual(head.state()["main"], "2M CALL")
        ok, _ = head.press("CH_UP")
        self.assertTrue(ok)
        self.assertEqual(self.backend.status.channel, 2)
        ok, msg = head.press("PF1")
        self.assertFalse(ok)
        ok, _ = head.press("POWER")
        self.assertFalse(ok)

    def test_level2_display_and_keys(self):
        link = FakeLink()
        cb = vch.Codebook()
        cb.keys["MON"] = [{"dt": 0, "hex": "c0de"}]
        cb.display = [vch.DisplayRule("main", "21", 1, 14)]
        head = vch.VirtualHead("tap", cb, radio=self.radio, ctl=self.backend, hb_link=link)
        frame = (b"\x21" + b"HELLO WORLD   ").hex()
        for cb_ in link.listeners:
            cb_(f"HB rx ? 123 {frame}")
        self.assertEqual(head.state()["main"], "HELLO WORLD   ")
        ok, _ = head.press("MON")
        self.assertTrue(ok)
        time.sleep(0.1)
        self.assertEqual(link.sent, ["HBTX D c0de"])

    def test_http(self):
        head = vch.VirtualHead("dbonly", vch.Codebook(), radio=self.radio, ctl=self.backend)
        srv = vch.PanelServer(("127.0.0.1", 0), head, token="s3cret")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            page = urllib.request.urlopen(base + "/").read()
            self.assertIn(b"virtual control head", page)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + "/api/state")
            st = json.load(urllib.request.urlopen(base + "/api/state?token=s3cret"))
            self.assertEqual(st["level"], 1)
            req = urllib.request.Request(base + "/api/channel?token=s3cret", method="POST",
                                         data=json.dumps({"n": 3}).encode(),
                                         headers={"Content-Type": "application/json"})
            r = json.load(urllib.request.urlopen(req))
            self.assertTrue(r["ok"])
            self.assertEqual(self.backend.status.channel, 3)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
