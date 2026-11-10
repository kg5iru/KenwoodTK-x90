# Virtual control head for the TK-890 (and TK-690/790)

The **virtual control head** (VCH) is a browser panel laid out like a
KCH-11 (LCD, TX/BUSY LEDs, channel knob, GR ◀ ▶, MON, SCN, SP, PF1–PF9,
POWER). Depending on how it is attached, it either works alongside the
radio's real head or replaces the head altogether. It is part of
`tkx90-catd`, so the same node that runs the AllStarLink remote base
serves it at `http://<node>:8073/`.

## How the real head works (from the service manual)

The KCH-10/11 is a small computer of its own (NEC µPD78064, *Display Unit
IC4*). It scans the keys, the channel encoder, the volume pot, PTT and
HOOK, and drives the LCD. Per the manual, key and knob changes "are
converted to serial data and sent to the control unit", and the display
content comes back from the radio the same way. The head and the radio
are connected by the 11-wire W501 lead (Kenwood E37-0787-05 for KCH-10):

| W501 / CN1 | Name | Function |
|---:|---|---|
| 1 / 11 | RS2 | remote speaker |
| 2 / 10 | RS1 | remote speaker |
| 3 / 9 | SB | 13.6 V after the power switch (powers the head) |
| 4 / 8 | IGN | ignition sense |
| 5 / 7 | PS | power-switch input to the radio |
| 6 / 6 | **TRD** | **the head bus**: one-wire serial, both directions |
| 7 / 5 | 1/2 | head 1 / head 2 select |
| 8 / 4 | RST | reset from radio to head |
| 9 / 3 | E | ground |
| 10 / 2 | MIC | microphone audio to the radio |
| 11 / 1 | ME | mic ground |

Facts from the KCH-11 schematic (circuit diagram, manual p. 88) that
shape this design:

* **TRD is a wired-AND, half-duplex UART bus.** The head CPU's RXD
  (pin 75) connects straight to TRD. Its TXD (pin 76) drives TRD through
  Q9 and Q10 (DTC114TU), two inversions, so the head pulls TRD low as an
  open collector, non-inverted. The pull-up is on the radio side. Every
  device hears its own transmissions, and two talkers collide.
* The same TRD also appears on pin 6 of the front 12-pin mic connector.
  This repository's programmer cable uses it at 9600 baud. The notebook
  reports that "replaying commands received" there changes the display,
  group and channel.
* **POWER (S1) is a momentary contact from PS to ground.** It toggles a
  flip-flop (IC501) in the radio.
* The volume pot goes to an ADC pin on the head CPU (pin 100 `VOL`), so
  volume is also carried as serial data.

The byte-level message format on TRD is **not documented anywhere I
could find**. The VCH does not guess at it. It learns from your radio
which frames a key press produces and where the display text sits (see
*Learning* below), and the levels are arranged so that something useful
works before any learning.

## Three levels

| | Level 1 "dbonly" | Level 2 "tap" | Level 3 "bridge" |
|---|---|---|---|
| Extra hardware | none (the DB-25 interface board) | + J3 head-bus tap on the same board | + inline bridge board on the W501 lead |
| Real head | stays (optional) | stays, required while learning | stays, or removed ("headless") |
| Display | channel name/number from the plan | mirrored from the head bus (learned) | mirrored, with direction known |
| Channel knob | steps through Remote Channels 1–15 | real CH up/down (learned), or plan | same |
| Keys (PF, MON, SCN, GR) | – | learned frames are replayed | same |
| TX / BUSY LEDs, S-meter | DB-25 TXS, SQ, RSI | same | same |
| SP (speaker mute) | DB-25 SPM | same | same |
| POWER | – | – | yes (PS pulled to ground) |
| Depends on undocumented protocol | **no** | yes (learned) | yes (learned) |

Level 1 is ready to use now. Levels 2 and 3 are designs plus tooling.
They will work only as far as the learned codebook captures the head
protocol, so expect some iteration on a real radio.

## Level 2: head-bus tap (on the DB-25 interface board)

Extend J3 on the interface board (`INTERFACE.md`) from receive-only to
receive and transmit:

```
                         5 V-tolerant RX                 open-drain TX
 J3-1 TRD ──┬── R7 22 kΩ ──┬──► GP17 (UART1 RX)     ┌── Q10 2N7002 drain (47 Ω)
            │              R8 33 kΩ                 │   gate ◄─ 100 Ω ◄─ GP20 (UART1 TX,
            │              GND                      │          100 kΩ to GND   inverted)
            └───────────────────────────────────────┘   source ─ GND
 J3-2 GND ── DB25-7 / 12-pin pin 5 (Earth)
```

* UART1 TX is set up with `invert=UART.INV_TX`. An idle UART (mark) holds
  the gate low, so the line stays released. A 0 bit pulls TRD low, the
  same way the head's own Q10 does. With the Pico unpowered, the 100 kΩ
  keeps Q10 off, so the tap can never hold the bus.
* Connect J3 to the **front 12-pin connector** (pin 6 TRD, pin 5 Earth)
  with the 3D-printed programmer connector from this repository
  (`programmer-connector/`). You don't need to open the radio or the
  head. The tap occupies the front mic jack, so a hand mic can't be
  plugged in at the same time.
* The firmware (`firmware/main.py` + `firmware/headbus.py`) frames
  traffic on idle gaps (`hb_gap_ms`, default 4 ms, about 4 bytes at
  9600 baud). It reports every frame, sends injected frames only after
  the bus has been idle (listen before talk), checks its own echo
  byte by byte, and retries on collision.
* In tap mode the source of a frame (radio or head) can't be seen on a
  single wire, so frames are reported as `?`. Learning still works
  because it compares activity before and after a key press.

## Level 3: inline bridge board (on the W501 head cable)

A second Raspberry Pi Pico (`firmware/vch_bridge.py`) sits *in series*
with TRD. All other W501 wires pass straight through.

```
 Radio W501 ───────────────────────────────────────────────────────────── Head CN1
   SB, IGN, RS1, RS2, 1/2, MIC, ME, E ─────── straight through ───────────
   PS ──────────────────────────┬────────────── straight through ─────────  (S1 still works)
                                ├── Q3 2N7002 ◄─ GP6        (POWER press)
                                └── D1 BAT54 ──► GP7        (PS sense)
   RST ─────────────────────────┬────────────── straight through ─────────
                                └── D2 BAT54 ──► GP8        (reset sense)
   SB ── 100 kΩ/27 kΩ ──► GP9 (radio on)       SB ── 78L05 ── 5V_R

   TRD (radio side) ─┬─ K1 NC ──────────────────────────────── TRD (head side)
                     │   K1 NO (energised: split)                   │
                     ├─ 22k/33k ─► GP1 (UART0 RX)                   ├─ 22k/33k ─► GP5 (UART1 RX)
                     └─ Q1 2N7002 ◄─ GP0 (UART0 TX, inv)            ├─ Q2 2N7002 ◄─ GP4 (UART1 TX, inv)
                                                                    └─ 4.7 kΩ to 5V_R (head-side pull-up)
   K1 coil (5 V, with flyback diode) ◄─ Q4 ◄─ GP10
```

* **Fail-safe bypass.** K1 is a small DPDT signal relay (for example
  Omron G6K-2F-Y-5V) that joins the two TRD halves when it is *not*
  energised. With the Pico off, unplugged or crashed, the radio and head
  talk directly, as if the board weren't there. The firmware energises
  K1 only in `HB BRIDGE` / `HB HEADLESS`.
* **Head-side pull-up.** The head has no pull-up on TRD (it relies on
  the radio's), so the split head segment gets a 4.7 kΩ pull-up to
  `5V_R`. `5V_R` comes from SB through a 78L05, so the pull-up is dead
  while the radio is off. That way the board never back-powers an off
  head through its RXD pin.
* **Forwarding.** Bytes from either side are copied to the other side
  within about 1 ms (cut-through). Each copy is put on the expected-echo
  list for that wire, so it isn't reported twice or bounced back. Both
  devices still see a normal bus, including their own echo. Every frame
  is reported with its source: `D` (radio) or `H` (head).
* **Headless.** In `HB HEADLESS` the head side is ignored. If the radio
  polls the head and expects an answer, a round trip through USB and the
  host would probably be too slow. So the bridge holds an
  **auto-responder table** (`AR ADD <match> <reply>`) and answers
  matching radio frames on the spot. The table is learned with the real
  head attached (`vch.py --bridge learn-auto`).
* **POWER.** GP6 → Q3 pulls PS to ground for 300 ms (`PWR`), the same as
  pressing S1. The panel's POWER key is disabled unless
  `allow_power = true`.
* **Connector.** Order a spare W501 lead (E37-0787-05 is listed for the
  KCH-10) and cut it, or splice into the lead behind the head. Measure
  the CN1 header pitch on your head before you order mating parts; I
  could not confirm it from the manual.

### Bridge BOM

| Qty | Part |
|---:|---|
| 1 | Raspberry Pi Pico |
| 1 | G6K-2F-Y-5V DPDT relay (K1) + 1N4148 flyback diode |
| 4 | 2N7002 (Q1–Q4), each with 100 Ω gate, 100 kΩ gate pull-down, 47 Ω drain |
| 2 | BAT54 (D1, D2) with 10 kΩ pull-ups to 3V3 |
| 2 | 22 kΩ + 33 kΩ dividers (TRD receive, one per side) |
| 1 | 100 kΩ + 27 kΩ divider (SB sense) |
| 1 | 78L05 + 2 × 100 nF (5V_R) |
| 1 | 4.7 kΩ (head-side TRD pull-up) |
| 1 | W501 lead or 11-pin splice |

## Learning (Levels 2 and 3)

Run the learning tools with `tkx90-catd` stopped, since they talk to the
Pico directly. The result is a JSON codebook (`/etc/tkx90/vch-codebook.json`).

```sh
cd /usr/local/lib/tkx90
CB=/etc/tkx90/vch-codebook.json
# 1. what the bus does when nobody touches anything
python3 vch.py -d /dev/tkx90ctl --tap -c $CB learn-baseline -s 30
# 2. each key, pressed once on the real head when prompted
for k in PF1 PF2 PF3 PF4 PF5 PF6 PF7 PF8 PF9 MON SCN GR_UP GR_DN CH_UP CH_DN; do
  python3 vch.py -d /dev/tkx90ctl --tap -c $CB learn-key $k
done
# 3. where the display text lives: show a known channel name on the head
python3 vch.py -d /dev/tkx90ctl --tap -c $CB learn-display "RPT 147.03"
python3 vch.py -d /dev/tkx90ctl --tap -c $CB learn-display "  4" --field sub
# 4. (bridge only) answers for headless operation, real head attached
python3 vch.py -d /dev/tkx90vch --bridge -c $CB learn-auto -s 120
# check / try
python3 vch.py -c $CB show
python3 vch.py -d /dev/tkx90ctl --tap -c $CB press MON
# raw capture for manual analysis
python3 vch.py -d /dev/tkx90ctl --tap capture -s 60 -o trd.jsonl
```

What the learners do:

* **learn-baseline** stores every frame seen while idle. Those frames
  (polls, periodic refreshes) are ignored when looking for key frames.
* **learn-key** captures a few seconds around one key press and keeps the
  frames that are new compared with the baseline (in bridge mode, only
  head-originated ones), together with their timing. Pressing the key in
  the panel replays exactly that sequence.
* **learn-display** searches the radio-to-head frames for the text you
  typed (as typed, upper-cased, and padded to 14 characters) and records
  the frame prefix and offset. The KCH-11's dot-matrix display is likely
  to carry ASCII. The KCH-10's 13-segment display may not, and in that
  case the tool says so and you need a raw capture.
* **learn-auto** pairs each radio frame with the head frame that answered
  it within 60 ms, and keeps the most common answer.

Limits to expect:

* Replayed key frames are fixed bytes. If the protocol includes a
  sequence number, a checksum over changing state, or a key-up frame
  with a varying value, simple replay will not be enough. `capture` gives
  you the data to work that out, and the codebook format can be extended.
* Volume is an analog value in the protocol. It isn't modelled. The
  remote base takes its audio from DB-25 DEO/AFO, which do not depend on
  the volume setting.
* Headless operation relies on the auto-responder covering every poll the
  radio makes. Check it on the bench with a dummy load before relying on
  it.

## Configuration

`/etc/tkx90/tkx90.toml`:

```toml
[vch]
enabled = true
mode = "dbonly"        # dbonly | tap | bridge
listen = "127.0.0.1"   # "0.0.0.0" for LAN access -- set a token then
port = 8073
token = ""
codebook = "/etc/tkx90/vch-codebook.json"
bridge_device = "/dev/tkx90vch"
headless = false
allow_power = false
```

The panel has no PTT button on purpose. Transmit stays with AllStarLink
and the URI, behind the controller's interlock and time-out timer.
