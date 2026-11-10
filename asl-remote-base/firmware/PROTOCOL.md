# TKX90CTL controller protocol

The Pico enumerates as a USB CDC serial port (`/dev/ttyACM*`; the udev
rule in `../asl3/99-tkx90.rules` adds `/dev/tkx90ctl`).  Baud rate does
not matter.  Commands are ASCII lines ending in CR or LF.  Every command
gets exactly one reply line ending in CR LF.  Lines that start with `EV`,
`U2R` or `TRD` are sent on their own (not as a reply) and the host
must accept them at any time.

| Command | Reply | Meaning |
|---|---|---|
| `ID` | `ID TKX90CTL 1.0` | Identify. Also sent once at boot. |
| `CH` / `CH?` | `CH n` | Current remote channel (0 = released, the radio uses its front-panel channel). |
| `CH n` | `CH n` / `ERR TX` / `ERR RANGE` | Select remote channel `n` (0–15). Refused while the radio is transmitting or PTT is asserted. PTT and COS are held off for `settle_ms` afterwards. |
| `ST` / `ST?` | `ST ch=n sq=b tx=b ptt=b cos=b tot=b ao=bbbb rssi=mV mm=b sm=b` | Status snapshot. `sq`=1 means the channel is busy (DB25-23 low). `tx`=1 means the radio is transmitting (DB25-11). `ptt`=1 means the URI wants to transmit. `cos`=1 means COS is asserted to the URI. `tot`=1 means the time-out timer tripped. `ao` lists AO1..AO4, where 1 means the open-collector output is ON (low). `rssi` is DB25-1 in millivolts. |
| `MM 0/1` | `MM b` | Mic mute (DB25-12 MCM pulled low when 1). With `mm_on_tx=1` (the default) the mic is also muted automatically whenever the Pico keys the radio. |
| `SM 0/1` | `SM b` | Speaker mute (DB25-24 SPM pulled low when 1). |
| `CFG` / `CFG?` | `CFG k=v ...` | Show configuration. |
| `CFG k=v [k=v ...]` | `CFG OK` | Change settings in RAM (`ch_invert`, `settle_ms`, `tot_s`, `boot_ch`, `cos_gate`, `cos_gate_tx`, `mm_on_tx`, `u2_baud`, `trd_baud`, `wdt_ms`). |
| `SAVE` | `SAVE OK` | Write the configuration to flash (`cfg.json`). |
| `U2 ON/OFF` | `U2 OK` | Turn forwarding of bytes received on DB25-9 TXD2 on or off (sent as `U2R <hex>`). |
| `U2 BAUD n` | `U2 OK` | Set the TXD2/RXD2 baud rate. |
| `U2W <hex>` | `U2W OK` | Send raw bytes to the radio's RXD2 (DB25-10). |
| `TRD ON/OFF` | `TRD OK` | Sniff the head-bus TRD line (optional J3) and report it as `TRD <hex>`. Receive only. |
| `RESET` | `RESET OK` | Reboot the Pico. |
| anything else | `ERR CMD` | |
| bad argument | `ERR ARG` | |

Unsolicited events:

* `EV TOT`: the transmit time-out timer tripped. PTT stays dropped
  until the URI releases its PTT.

## Channel-select encoding

Remote channel `n` (1–15) is sent in binary on CH Select A (bit 0, AI2)
through CH Select D (bit 3, AI5). A `1` bit turns its MOSFET on, which
pulls the radio's input LOW. `n = 0` leaves all four lines open, and the
radio then uses its front-panel channel.

The manual (TK-890 service manual, §36-1 "PF Input Port") says that with
all four lines "H or Open" the panel selects the channel. It does not say
which level is active. Bench-check this on your radio (see the
commissioning steps in `../README.md`). If the levels turn out to be
inverted, set `CFG ch_invert=15` and `SAVE`.
