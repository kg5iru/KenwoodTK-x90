# TK-x90 audio + CAT interface board

This board plugs into the TK-x90's rear **DB-25 accessory connector**. It
gives an AllStarLink node everything it needs to run the radio as a
remote base:

* audio in and out, plus PTT and COS, routed to a standard CM108-based
  USB Radio Interface (URI), and
* channel selection and radio status, handled by a Raspberry Pi Pico
  that shows up on the node as a USB serial "CAT" port.

You don't need the front 12-pin connector or any other cable. You only
open the radio to set the DI/MI jumper (see "Radio preparation").

> Signal names and pin numbers come from the TK-890 service manual
> (B51-8439-10), *Terminal Function*, "Acc 25 pin D-sub connector", and
> *Installation §8*. The TK-790 notebook in this repository uses the same
> pins (2/7/13/19) for its TNC adapter. The TK-690/790/890 all share the
> control-unit design, but check against your model's manual before you
> build.

## Block diagram

```
             TK-x90 rear DB-25 (female on radio, male plug on this board)
 ┌───────────────────────────────────────────────────────────────────────┐
 │ 19 DEO ──C1──RV1──┐                          ┌── J2-3 RX AUDIO ─┐     │
 │ 17 AFO ───────JP3─┘──────────────────────────┘                  │     │
 │ 13 MI/DI ──R3──RV2──C2──────────────────────── J2-2 TX AUDIO ──┤ URI │
 │ 25 ME  ─────────────────────────────────────── J2-1 AGND       │(CM108│
 │  2 AI1 (Ext PTT) ◄─ Q1 ◄─ GP6 ┐   JP1          ┌ J2-4 PTT ◄────┤ fob) │
 │                               └─ GP7 ◄─────────┘                │     │
 │ 23 SQ ──D1──► GP10 ──► Q8 ──JP2──────────────── J2-5 COS ──────┘     │
 │  3 AI2 CH Sel A ◄─ Q2 ◄─ GP2                                          │
 │  4 AI3 CH Sel B ◄─ Q3 ◄─ GP3        Raspberry Pi Pico (RP2040)        │
 │  5 AI4 CH Sel C ◄─ Q4 ◄─ GP4        MicroPython: firmware/main.py     │
 │  6 AI5 CH Sel D ◄─ Q5 ◄─ GP5        USB ──────────────► ASL3 node     │
 │ 12 MCM mic mute ◄─ Q6 ◄─ GP8                                          │
 │ 24 SPM spk mute ◄─ Q7 ◄─ GP9                                          │
 │ 11 TXS ──D2──► GP11                                                   │
 │ 20,8,21,22 AO1..4 ──D3..D6──► GP12..GP15                              │
 │  1 RSI ──R1──┬──► GP26/ADC0                                           │
 │              C3                                                       │
 │  9 TXD2 ──R5/R6 divider──► GP1 (UART0 RX)                             │
 │ 10 RXD2 ◄── Q9 (BSS138 level shifter) ◄── GP0 (UART0 TX)              │
 │  7,18 E ───────── GND                                                 │
 └───────────────────────────────────────────────────────────────────────┘
   J3 (optional, research): 12-pin head connector pin 6 TRD ─R7/R8─► GP17
```

## DB-25 accessory connector (radio side)

| Pin | Name | Dir | Used for | Notes from the manual |
|---:|---|:---:|---|---|
| 1 | RSI | O | RSSI → Pico ADC | ≈0.2 V at −130 dBm up to ≈1.6 V at −90 dBm |
| 2 | AI1 | I | **External PTT** | FPU-selectable PF input; program as *External PTT* |
| 3 | AI2 | I | **CH Select A** (bit 0) | PF input |
| 4 | AI3 | I | **CH Select B** (bit 1) | PF input |
| 5 | AI4 | I | **CH Select C** (bit 2) | PF input |
| 6 | AI5 | I | **CH Select D** (bit 3) | PF input |
| 7 | E | – | logic ground | |
| 8 | AO2 | O | status (optional) | PF output, open collector |
| 9 | TXD2 | O | research bridge | second serial port, purpose undocumented |
| 10 | RXD2 | I | research bridge | |
| 11 | TXS | O | TX sense → Pico | "H": TX |
| 12 | MCM | I | mic mute | "L": mute on |
| 13 | MI/DI | I | **TX audio** | DI: 10 kΩ, flat, DC-coupled, 2.5 Vpp @1 kHz ≈ 60 % dev; MI: 600 Ω, 5 mV ≈ 60 % dev, pre-emphasised |
| 14 | NC (SB) | – | not used | 13.6 V/0.5 A only after a board mod |
| 15, 16 | RSV1/2 | – | not used | |
| 17 | AFO | O | RX audio (alt.) | 125 mVrms at standard modulation, de-emphasised |
| 18 | E | – | audio ground | |
| 19 | DEO | O | **RX audio** | discriminator, 100 mVrms, ≤1 kΩ, DC-coupled, flat |
| 20 | AO1 | O | status (optional) | PF output |
| 21 | AO3 | O | status (optional) | PF output |
| 22 | AO4 | O | status (optional) | PF output |
| 23 | SQ | O | **COS** | "L": busy, "H": not busy |
| 24 | SPM | I | speaker mute | "L": mute on |
| 25 | ME | – | mic earth | TX audio return |

Per the manual, the five PF inputs on the DB-25 can each be set to:
*External HOOK, CH Select A–D, External PTT, Scan, Home Channel, Light
Sense, Repeater SW, External Monitor*. When "CH Select A–D" are all
"H or Open", the panel picks the channel. Otherwise the radio goes to
**Remote Channel 1–15**. This is the documented mechanism that the
remote-base design is built on.

## Circuit

All radio-facing logic is either **open-drain** (outputs) or
**diode-isolated** (inputs). The Pico never sources a voltage into the
radio, and the radio's 5 V logic never reaches a 3.3 V Pico pin.

### Outputs to the radio (Q1–Q8)

Each output uses one 2N7002 (or BSS138) N-MOSFET:

```
 GPn ──R(100 Ω)──┬── G          drain ──R(47 Ω)── DB-25 pin
                 R(100 kΩ)      source ── GND
                 GND
```

* The 100 kΩ gate pull-down keeps every line **released** while the Pico
  is in reset or unplugged. So: no PTT, CH Select open (the radio stays
  on its panel channel), no mute.
* The radio's own pull-ups set the high level. The MOSFET only pulls the
  line low.
* The 47 Ω in the drain limits current if the cable is plugged into the
  wrong port.

| Ref | Pico | Radio |
|---|---|---|
| Q1 | GP6 | DB25-2 AI1 External PTT |
| Q2–Q5 | GP2–GP5 | DB25-3..6 AI2..AI5 CH Select A..D |
| Q6 | GP8 | DB25-12 MCM |
| Q7 | GP9 | DB25-24 SPM |
| Q8 | GP16 | J2-5 COS to the URI (via JP2) |

### Inputs from the radio (D1–D6)

```
 3V3 ──R(10 kΩ)──┬── GPn
                 │
 DB-25 pin ──|◄──┘     (BAT54 / 1N5817 Schottky, cathode to the radio)
```

When the radio pulls the line low, the Schottky diode conducts and the
pin reads 0 (about 0.3–0.5 V). When the radio drives it high (up to
5 V) or leaves it open, the diode is reverse-biased and the 10 kΩ holds
the pin at 3.3 V. This works for both push-pull and open-collector
outputs.

| Ref | Radio | Pico | Meaning of a LOW pin |
|---|---|---|---|
| D1 | 23 SQ | GP10 | busy (COS) |
| D2 | 11 TXS | GP11 | *not* transmitting |
| D3–D6 | 20/8/21/22 AO1..AO4 | GP12..GP15 | output ON |

### RSSI

DB25-1 → R1 10 kΩ → GP26 (ADC0), with C3 100 nF to GND. The full range
(0.2–1.6 V) is well inside the ADC range.

### Second serial port (TXD2/RXD2), research only

* TXD2 (DB25-9) → R5 22 kΩ → GP1, with R6 33 kΩ from GP1 to GND. 5 V
  divides down to 3.0 V.
* GP0 → BSS138 level shifter (Q9, 10 kΩ pull-ups to 3V3 and to the
  Pico's VBUS 5 V) → RXD2 (DB25-10).

Nobody has documented what the radio does on this port. The firmware
exposes it as a raw byte bridge (`U2 ON`, `U2W <hex>`) so you can
explore it. See the README roadmap.

### Audio

**RX:** DEO (DB25-19) → C1 1 µF film → RV1 10 kΩ trimmer (to AGND, wiper
out) → J2-3 RX AUDIO to the URI. DEO carries a DC bias, so C1 is
required. JP3 selects AFO (DB25-17, already de-emphasised) instead. If
you do, set `deemphasis = no` in simpleusb.conf.

**TX (DI mode, recommended):** J2-2 TX AUDIO from the URI → C2 4.7 µF →
RV2 10 kΩ trimmer → R3 1 kΩ → DB25-13 MI/DI. Return to DB25-25 ME.
DI is flat, so set `preemphasis = yes` in simpleusb.conf. A CM108's
line output (≈1 Vrms max) gives full deviation with RV2 near mid-scale.

**TX (MI mode):** if your radio is jumpered for MI (R640 fitted), the
input wants millivolts. Fit JP4 to switch in R4 4.7 kΩ / R9 47 Ω (about
−40 dB) after RV2, and set `preemphasis = no` (the radio pre-emphasises).

Keep the audio grounds (DB25-18 E for RX, DB25-25 ME for TX) separate
from the logic ground (DB25-7) on the board. Join them at the DB-25
shell only.

### PTT and COS routing (JP1, JP2)

| Jumper | PICO (default) | DIRECT |
|---|---|---|
| JP1 PTT | URI PTT → GP7. The Pico copies it to Q1/AI1, adding an interlock during channel changes, a time-out timer (`tot_s`) and auto mic-mute. | URI PTT goes straight to AI1. The interface keeps working with no Pico. |
| JP2 COS | Q8, driven from SQ by the Pico. COS is suppressed while switching channels and while transmitting. | SQ → BAT54 → URI COS. |

The URI's PTT output must be open-collector/active-low (all
CM108-based URIs are), and its COS input active-low (use
`carrierfrom = usbinvert`).

### URI header J2

| J2 | Signal |
|---|---|
| 1 | AGND |
| 2 | TX AUDIO (URI speaker/line out → radio) |
| 3 | RX AUDIO (radio → URI mic/line in) |
| 4 | PTT (from URI, open collector, low = TX) |
| 5 | COS (to URI, open drain, low = busy) |
| 6 | CTCSS (not used, NC) |

Map these to your URI (DMK URI, RA-40, AllScan UCI90, a modified CM108
fob, ...) using its documentation.

### J3, head-bus sniffer (optional)

The control head and the radio talk on the single-wire TRD line (12-pin
connector pin 6, 9600 baud, the same line used for programming). With a
3D-printed front connector (see `../../microphone-connector`) you can
tap TRD through R7 22 kΩ / R8 33 kΩ into GP17 (UART1 RX). `TRD ON` then
streams the traffic. This input is receive-only, so the board can never
drive the head bus.

## BOM

| Qty | Ref | Part |
|---:|---|---|
| 1 | U1 | Raspberry Pi Pico (RP2040), or Pico W |
| 1 | J1 | DB-25 **male** solder-cup or right-angle PCB plug, with #4-40 jack screws |
| 1 | J2 | 6-pin 2.54 mm header (to the URI) |
| 1 | J3 | 2-pin header (optional TRD tap) |
| 9 | Q1–Q9 | 2N7002 or BSS138, SOT-23 |
| 6 | D1–D6 | BAT54 (SOT-23) or 1N5817 |
| 8 | – | 100 Ω gate resistors |
| 8 | – | 100 kΩ gate pull-downs |
| 8 | – | 47 Ω drain resistors |
| 8 | – | 10 kΩ input pull-ups (plus 2 for the Q9 shifter) |
| 1 | R1 | 10 kΩ |
| 2 | R5, R7 | 22 kΩ |
| 2 | R6, R8 | 33 kΩ |
| 1 | R3 | 1 kΩ |
| 1 | R4 | 4.7 kΩ (MI option) |
| 1 | R9 | 47 Ω (MI option) |
| 2 | RV1, RV2 | 10 kΩ trimmer (multi-turn recommended) |
| 1 | C1 | 1 µF film |
| 1 | C2 | 4.7 µF (film or bipolar electrolytic) |
| 1 | C3 | 100 nF |
| 4 | JP1–JP4 | 3-pin header + shunt (JP4 2-pin) |
| 1 | – | CM108-based URI (not on this board) |

The circuit is small enough for perfboard behind a 3D-printed DB-25
backshell. The parametric shell used by the TNC adapter in the main
notebook works if you make it deeper.

## Radio preparation

1. **DI/MI jumper.** The manual's default is DI (R641 fitted, R640
   open), but the notebook found most radios set to MI. For this
   interface, move the 0 Ω jumper to **R641 (DI)**. This is the same
   change as the notebook's 9600-baud mod. If you would rather keep MI,
   fit JP4.
2. **Ignition sense.** Disable it (R506 → R504), or wire the 9-pin
   ignition line, as the notebook describes.
3. **Speaker.** Optional. A remote base doesn't need local audio. SPM
   (DB25-24) can mute it from software (`SM 1`).

## KPG-44D programming

Read the radio first (as described in the notebook), then:

1. **Edit → Function Port → Input** (DB-25 "Radio 1" inputs):
   * AI1 → **External PTT**
   * AI2 → **CH Select A**
   * AI3 → **CH Select B**
   * AI4 → **CH Select C**
   * AI5 → **CH Select D**
2. **Remote Channels 1–15:** assign each remote channel to a
   programmed group/channel with the frequency, tones, power and
   bandwidth you want. The table in `../host/tkx90.toml` must describe
   the **same** channels. The menu location differs between KPG-44D
   versions. Look for "Remote Channel" in the channel or optional
   feature settings.
3. **Time-out timer:** keep a radio TOT as a backstop (for example 180 s).
   The Pico has its own TOT as well.
4. **Squelch:** set the per-channel QT/DQT decode as needed. Check on the
   bench whether the DB-25 SQ line follows carrier squelch or QT-qualified
   squelch on your firmware (commissioning step 6).
5. Write the data to the radio.

A front-panel channel stays usable. When the Pico releases the
CH Select lines (channel 0), the radio returns to whatever the panel
selects.
