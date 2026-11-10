# TK-x90 as an AllStarLink 3 frequency-agile remote base

This directory has a complete design for running a Kenwood **TK-690,
TK-790 or TK-890** as an **ASL3 remote base** with a CAT interface:

| Part | Where | What it does |
|---|---|---|
| Audio + CAT interface board | [`hardware/INTERFACE.md`](hardware/INTERFACE.md) | DB-25 plug with a Raspberry Pi Pico and audio/PTT/COS routing to a CM108 URI |
| Controller firmware | [`firmware/main.py`](firmware/main.py), [`firmware/PROTOCOL.md`](firmware/PROTOCOL.md) | MicroPython. Drives channel select, gates PTT, reads SQ/TX/RSSI |
| CAT daemon | [`host/tkx90_catd.py`](host/tkx90_catd.py), [`host/tkx90.toml`](host/tkx90.toml) | Emulates the Kenwood rig app_rpt supports, and serves the Hamlib rigctld protocol |
| AllStarLink 3 config | [`asl3/`](asl3) | rpt.conf, simpleusb.conf, udev rule, systemd unit, installer |
| Hamlib config | [`hamlib/README.md`](hamlib/README.md) | `rigctl -m 2` usage and the supported command set |
| Virtual control head | [`hardware/VIRTUAL_HEAD.md`](hardware/VIRTUAL_HEAD.md), [`host/vch.py`](host/vch.py), [`firmware/headbus.py`](firmware/headbus.py), [`firmware/vch_bridge.py`](firmware/vch_bridge.py) | Browser front panel laid out like a KCH-11. It works with the DB-25 board alone, or with the head bus tapped or bridged |
| Tests | [`tests/`](tests) | Daemon (app_rpt wire format, pty, TCP), firmware logic, head-bus engine, virtual head |

## The key constraint: the TK-x90 has no CAT port

The TK-x90 has no VFO and no documented remote-control protocol. Its
frequencies live in channels that KPG-44D writes over the programming
interface. Two things make a remote base possible anyway:

1. **The radio's documented hardware channel select.** Four DB-25 PF
   inputs can be programmed as *CH Select A–D*. The binary code on them
   picks one of **15 Remote Channels** (all lines open hands control back
   to the front panel). This needs no firmware hacks, and switching takes
   milliseconds.
2. **A CAT front end that makes those channels look like a tunable
   radio.** `tkx90-catd` accepts ordinary "set frequency / offset /
   tone" requests from app_rpt and from Hamlib. It matches each request
   against the channel plan and selects the right Remote Channel. A
   request for anything else is refused cleanly: app_rpt announces
   *invalid frequency*, and Hamlib returns `RPRT -1`.

So the radio is **frequency-agile across a plan of up to 15 channels
(any mix of simplex, repeater pairs, tones and power levels)**. It is not
a continuously tunable VFO. For a remote base this is usually what you
want anyway: the radio can only ever transmit on frequencies that you
programmed and checked. "Roadmap" below covers the routes to true VFO
tuning.

## Architecture

```
  ASL user (phone/radio/IAX)                 Hamlib clients
       │ DTMF *1147*03*3 ...                 (rigctl, flrig, WSJT-X, scripts)
       ▼                                            │ TCP 4532 (rigctld protocol)
 ┌─────────────────────── ASL3 node (Pi / x86) ─────┼──────────────────────┐
 │ app_rpt  remote=kenwood                          │                      │
 │   ioport=/run/tkx90/asl-cat ──pty──► tkx90-catd ◄┘                      │
 │                                     │  channel plan (tkx90.toml)        │
 │                                     │  match → Remote Channel n         │
 │ chan_simpleusb ◄─USB audio─┐        └──USB CDC (/dev/tkx90ctl)──┐      │
 └────────────────────────────┼───────────────────────────────────┼──────┘
                              │                                   │
                     CM108 URI (audio, PTT, COS)          Raspberry Pi Pico
                              │                                   │
                              └──────── interface board ──────────┘
                                             │ DB-25
                                      TK-690/790/890
```

* **Audio, PTT and COS** go through a standard CM108 URI and
  `chan_simpleusb`, the same as any ASL3 node. The Pico gates PTT
  (channel-change interlock and TOT). Jumpers JP1/JP2 bypass it if you
  want.
* **app_rpt** drives the radio with its built-in `remote = kenwood` rig
  type, which is the TM-V71-style `VMC` / `VW` / `RBN` / `PC` command set
  (`apps/app_rpt/rpt_rig.c`, `setkenwood()`). `tkx90-catd` answers those
  commands on a pseudo-terminal. app_rpt needs no patches.
* **Hamlib** clients connect to the rigctld-protocol server inside
  `tkx90-catd` with model 2 (NET rigctl). Only one process can own the
  Pico's serial port, so the daemon owns it and serves both app_rpt and
  Hamlib.

## Build and install

1. **Build the interface** from [`hardware/INTERFACE.md`](hardware/INTERFACE.md).
2. **Prepare and program the radio** with KPG-44D: set the DI jumper, set
   AI1 = External PTT, AI2–AI5 = CH Select A–D, and set up Remote Channels
   1–15 (see *Radio preparation* and *KPG-44D programming* in the hardware
   doc).
3. **Flash the Pico.** Install MicroPython for the Pico, then
   `mpremote cp firmware/headbus.py :headbus.py`,
   `mpremote cp firmware/main.py :main.py` and `mpremote reset`.
4. **Install the daemon** on the ASL3 node:
   ```sh
   sudo asl-remote-base/asl3/install.sh
   sudoedit /etc/tkx90/tkx90.toml          # describe the same 15 channels
   python3 /usr/local/lib/tkx90/tkx90_catd.py -c /etc/tkx90/tkx90.toml --check
   ```
5. **Configure ASL3:**
   * `modules.conf`: `load => chan_simpleusb.so` and `load => res_usbradio.so`.
   * Add [`asl3/simpleusb.conf.remote-base`](asl3/simpleusb.conf.remote-base) and
     [`asl3/rpt.conf.remote-base`](asl3/rpt.conf.remote-base) (set your node number).
   * Generate the memory stanza from your plan:
     `python3 /usr/local/lib/tkx90/tkx90_catd.py -c /etc/tkx90/tkx90.toml --asl-memory`
     and paste it over `[memory-tkx90]`.
   * `sudo systemctl restart tkx90-catd asterisk`
6. **Set the audio levels** with `asl-tune-usb` (TX deviation checked on an
   SDR or a service monitor; RX level with a known on-channel signal).

## Using it

From a node connected to the remote base (`*3<remote-node>`), with the
`[functions-remote]` stanza provided:

| DTMF | Action |
|---|---|
| `*0nn` | recall memory `nn`, e.g. `*002` |
| `*1MMM*kkk*o` | set frequency and offset: `o` = 1 minus, 2 simplex, 3 plus. Example: `*1147*03*3` = 147.030 + |
| `*2nnn*n` | set RX (and TX) CTCSS, e.g. `*2103*5` = 103.5 |
| `*3nnn*n` | set TX CTCSS |
| `*40` / `*41` | RX tone squelch off / on |
| `*42` / `*43` | TX tone off / on |
| `*44` / `*46` | low / high power |
| `*51` / `*52` | long / short status |

Every change goes through the same plan matcher. Example: dial
`*1147*03*3`. That selects Remote Channel 3 (147.030 +, tone 103.5).
Then `*2103*5` (PL 103.5) followed by `*41` (RX PL on) moves to Remote
Channel 4, the same repeater with tone squelch. With `mode = "best"` (the default) only the RX frequency has to
match, and shift, offset and tones choose between channels that share
it. With `mode = "strict"` the TX frequency and TX tone must match too.

From Hamlib: `rigctl -m 2 -r <node>:4532 F 147030000` (see
[`hamlib/README.md`](hamlib/README.md)).

## Commissioning checklist

1. `mpremote` REPL or `screen /dev/tkx90ctl`: `ID` → `ID TKX90CTL 1.0`;
   `ST` returns status.
2. Radio off, board plugged in: `CH 5` → DB25-3 and DB25-5 measure about
   0 Ω to ground. `CH 0` → all open.
3. Radio on: `CH 1` … `CH 15` → the radio changes to each Remote Channel.
   If it doesn't (or the pattern is wrong), the active level is the
   opposite: `CFG ch_invert=15`, `SAVE`.
4. Key the URI PTT (`asl-tune-usb` test tone): the radio transmits and
   `ST` shows `tx=1`. `CH 2` during TX → `ERR TX`.
5. Open the squelch on a signal: `ST` shows `sq=1`, and the URI sees COS
   (check in `asl-tune-usb`). If it reads backwards, the SQ line polarity
   on your firmware is inverted. Set `carrierfrom = usb` and use JP2 =
   DIRECT, or swap the logic in `tick()`.
6. On a tone-squelch channel, send a carrier **without** the tone. If
   `sq` goes to 1 anyway, SQ is carrier-only. Then either use carrier
   squelch channels or use an AO output programmed for the QT decode
   indication (if your KPG-44D offers one) as the COS source.
7. Once commissioned, turn on the Pico watchdog: `CFG wdt_ms=4000`, `SAVE`.

## Known limitations

* **At most 15 frequencies** at a time, set by the radio's Remote Channel
  count. If you need different ones, reprogram with KPG-44D and update
  `tkx90.toml` (the generator keeps app_rpt's `[memory]` in step).
* **app_rpt's band check for `remote = kenwood`** accepts 145–148 MHz
  and 430–450 MHz for **DTMF frequency entry**. Because app_rpt cuts the
  decimals down to three digits, 144.xxx is always rejected. Memory
  recall (`*0nn`) does not go through that check. Use memories for
  144.xxx, for TK-890 channels above 450 MHz, and for the **TK-690 (6 m
  and low band)**. Hamlib has no such limit. The advertised range comes
  from `[hamlib]` in `tkx90.toml`.
* **Power, bandwidth and tones are per channel.** `*44`/`*46` and the
  PL commands only work if the plan has a matching variant. Otherwise the
  request is ignored (power) or refused (tones).
* **app_rpt's own state** is what it announces in status messages. The
  daemon selects the closest real channel. In `best` mode that channel
  can carry a different tone than the one requested. Use `strict` if you
  want such requests refused instead.

## Roadmap: getting to true VFO tuning

The board already has hooks for research into both possible routes:

1. **Scratch-channel programming.** KPG-44D writes channel data over
   the one-wire TRD interface. If someone documents that protocol (see
   CHIRP issue #3363 for early work), the daemon could rewrite one
   "scratch" Remote Channel and reselect it. The cost is a few seconds
   per QSY, EEPROM wear, and the risk inherent in programming mode. This
   would fit in `tkx90_catd.py` as another backend behind `Radio.select()`.
2. **Head-bus or TXD2/RXD2 commands.** The notebook reports that
   replaying head-bus TRD messages changes group/channel. The virtual
   control head (below) has the tooling for this: the J3 tap and the
   inline bridge capture and replay TRD traffic. The Pico can also
   bridge the undocumented DB-25 serial port (`U2 ON`, `U2W <hex>`). Neither of these gives
   VFO tuning on stock firmware, but they could replace the 15-channel
   limit with group+channel selection.
3. **Firmware modification** of the M16C (M30625FGAGP), as described in
   the notebook. This is the only route to a real VFO and CAT command set.

## Virtual control head

`tkx90-catd` can also serve a browser front panel for the radio at
`http://<node>:8073/`. Set `[vch] enabled = true` in `tkx90.toml`. The
panel has the KCH-11 layout: LCD, TX/BUSY LEDs, channel knob, GR ◀ ▶,
MON, SCN, SP, PF1–PF9 and POWER. It runs at one of three levels:

1. **DB-25 only** (works with the interface board as built): the knob
   steps through the Remote Channels, the LEDs and S-meter come from
   TXS, SQ and RSSI, and SP mutes the speaker.
2. **Head-bus tap**: J3 is connected to the front 12-pin connector. The
   real head stays on, and the panel mirrors its display and presses
   its keys using frames learned from your radio.
3. **Inline bridge**: a second Pico sits on the W501 head cable. Frames
   have a known direction, POWER works, and the radio can run with the
   head removed. A relay bypasses the bridge whenever it isn't running.

The radio-to-head protocol is undocumented, so levels 2 and 3 run from
a codebook you learn with `vch.py` (a guided, key-by-key procedure).
Details, schematics and limits are in
[`hardware/VIRTUAL_HEAD.md`](hardware/VIRTUAL_HEAD.md).

## Tests

```sh
python3 -m unittest discover -s asl-remote-base/tests -v
```

The daemon tests replay the exact `VW` strings that app_rpt's
`setkenwood()` produces (including its decimal truncation), drive the
pty byte-by-byte with app_rpt's framing, and exercise the rigctld TCP
server. The rigctld side was also checked by hand against Hamlib 4.5.5
`rigctl -m 2`. The firmware tests run `main.py` on CPython against a stub
`machine` module (channel bits, inversion, PTT settle interlock, TOT,
COS gating). Nothing here has been run on a real radio yet. The
commissioning checklist above is the first hardware test.

## License

CC-BY-SA 4.0, same as the rest of this repository.
