# Head-bus (TRD) engine for the TK-x90 virtual control head.
#
# The KCH-10/11 control head and the radio share a single wire, TRD
# (W501 pin 6, also pin 6 of the front 12-pin connector).  From the
# KCH-11 schematic in the TK-890 service manual: the head CPU's RXD is
# wired straight to TRD, and its TXD drives TRD through two DTC114
# transistors (Q9/Q10) as an open-collector output.  The pull-up is in
# the radio.  So TRD is a wired-AND, half-duplex UART bus: every
# transmitter hears its own bytes, and simultaneous transmitters collide.
#
# This module is pure logic.  UART objects and a millisecond clock are
# passed in, so it runs on the Pico (MicroPython) and on a PC under test.
#
#   FrameAssembler : splits a byte stream into frames on idle gaps
#   Segment        : one physical TRD wire: RX, open-drain TX, a FIFO of
#                    bytes we wrote (so our own echo is not reported),
#                    listen-before-talk and collision retry
#   Tap            : one Segment on the shared bus, next to a real head
#   Bridge         : two Segments (deck side, head side) with the Pico
#                    forwarding between them, so every frame has a known
#                    direction, plus an auto-responder for running with
#                    no head attached
#
# The TRD message format is not documented.  Nothing here assumes one:
# frames are opaque byte strings that the host learns (../host/vch.py).

try:
    import ubinascii as binascii
except ImportError:
    import binascii


def hexs(b):
    return binascii.hexlify(bytes(b)).decode()


class FrameAssembler:
    """Collects bytes and emits a frame after `gap_ms` of silence."""

    def __init__(self, gap_ms=4, max_len=64):
        self.gap_ms = gap_ms
        self.max_len = max_len
        self.buf = bytearray()
        self.last = 0

    def push(self, data, now):
        self.buf.extend(data)
        self.last = now
        if len(self.buf) >= self.max_len:
            return self.flush()
        return None

    def poll(self, now):
        if self.buf and now - self.last >= self.gap_ms:
            return self.flush()
        return None

    def flush(self):
        f = bytes(self.buf)
        self.buf = bytearray()
        return f


class Segment:
    """One TRD wire.  TX is a UART TX pin with inverted output driving an
    N-MOSFET to ground, so the line is released when idle."""

    def __init__(self, uart, gap_ms=4, lbt_ms=6, retries=3, ms_per_byte=2):
        self.uart = uart
        self.asm = FrameAssembler(gap_ms)
        self.lbt_ms = lbt_ms
        self.retries = retries
        self.ms_per_byte = ms_per_byte
        self.echo = bytearray()   # everything we wrote, not yet heard back
        self.queue = []           # [frame, tries] waiting for an idle bus
        self.sending = None       # [frame, tries] currently on the wire
        self.send_t = 0
        self.last_rx = -1000
        self.collisions = 0

    def send(self, frame):
        self.queue.append([bytes(frame), 0])

    def write_raw(self, data):
        """Forwarded bytes (bridge): put on the wire now, expect the echo."""
        self.uart.write(data)
        self.echo.extend(data)

    def _collision(self, ev):
        self.collisions += 1
        self.echo = bytearray()
        item = self.sending
        self.sending = None
        if item is not None:
            item[1] += 1
            if item[1] > self.retries:
                ev.append(("drop", item[0]))
            else:
                self.queue.insert(0, item)
                ev.append(("coll", item[0]))

    def service(self, now, allow_tx=True):
        """Returns (foreign_bytes, events).  foreign_bytes are bytes put on
        this wire by the other device; events are ('rx', frame),
        ('tx', frame), ('coll', frame), ('drop', frame)."""
        ev = []
        n = self.uart.any()
        data = self.uart.read(n) if n else b""
        foreign = bytearray()
        for b in data or b"":
            if self.echo:
                if b == self.echo[0]:
                    self.echo.pop(0)
                    continue
                self._collision(ev)
            foreign.append(b)
        if data:
            self.last_rx = now
        if foreign:
            f = self.asm.push(foreign, now)
            if f:
                ev.append(("rx", f))
        f = self.asm.poll(now)
        if f:
            ev.append(("rx", f))
        if self.sending is not None:
            if not self.echo:
                ev.append(("tx", self.sending[0]))
                self.sending = None
            elif now - self.send_t > 20 + self.ms_per_byte * len(self.sending[0]):
                # Never heard ourselves: TX path broken or bus held low.
                self.echo = bytearray()
                ev.append(("drop", self.sending[0]))
                self.sending = None
        if (allow_tx and self.queue and self.sending is None and not self.echo
                and not self.asm.buf and now - self.last_rx >= self.lbt_ms):
            self.sending = self.queue.pop(0)
            self.echo = bytearray(self.sending[0])
            self.send_t = now
            self.last_rx = now
            self.uart.write(self.sending[0])
        return bytes(foreign), ev


class Tap:
    """Virtual head sharing the bus with a real head.  Direction of
    received frames is unknown ('?')."""

    def __init__(self, uart, **kw):
        self.seg = Segment(uart, **kw)

    def send(self, toward, frame):
        self.seg.send(frame)

    def service(self, now):
        _, ev = self.seg.service(now)
        return [(k, "?", f) for k, f in ev]


class Bridge:
    """Pico inline between the radio (deck) and the head.

    Bytes from one wire are copied to the other as they arrive, so each
    device still sees an ordinary bus.  Frames are reported with their
    source: 'D' = deck (radio), 'H' = head.  `inject('D', f)` sends f to
    the deck as if the head had sent it; `inject('H', f)` the reverse.

    With `headless` set, the head wire is ignored and deck frames that
    start with a learned prefix are answered at once by the
    auto-responder, because a round trip through the host would be too
    slow if the radio polls the head."""

    def __init__(self, deck_uart, head_uart, gap_ms=4):
        self.deck = Segment(deck_uart, gap_ms=gap_ms, lbt_ms=gap_ms)
        self.head = Segment(head_uart, gap_ms=gap_ms, lbt_ms=gap_ms)
        self.headless = False
        self.auto = []   # (match_prefix, reply)

    def add_auto(self, match, reply):
        self.auto.append((bytes(match), bytes(reply)))

    def clear_auto(self):
        self.auto = []

    def inject(self, toward, frame):
        (self.deck if toward == "D" else self.head).send(frame)

    def service(self, now):
        out = []
        # Only inject on a wire while nothing is being forwarded onto it.
        d_bytes, d_ev = self.deck.service(now, allow_tx=not self.head.asm.buf)
        if d_bytes and not self.headless:
            self.head.write_raw(d_bytes)
        h_bytes, h_ev = self.head.service(
            now, allow_tx=not self.deck.asm.buf and not self.headless)
        if h_bytes and not self.headless:
            self.deck.write_raw(h_bytes)
        for kind, f in d_ev:
            if kind == "rx":
                out.append(("rx", "D", f))
                if self.headless:
                    for match, reply in self.auto:
                        if f[:len(match)] == match:
                            self.deck.send(reply)
                            out.append(("auto", "H", reply))
                            break
            else:
                # our injection toward the deck (on behalf of the head)
                out.append((kind, "H", f))
        for kind, f in h_ev:
            if self.headless:
                continue
            if kind == "rx":
                out.append(("rx", "H", f))
            else:
                out.append((kind, "D", f))
        return out
