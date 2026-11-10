# Hamlib with the TK-x90 remote base

`tkx90-catd` speaks the **rigctld network protocol** on TCP port 4532.
Any Hamlib client can connect to it with Hamlib's `NET rigctl` model
(model number **2**). Do not run a separate `rigctld`, because the
daemon already does that job.

```sh
# one-shot commands
rigctl -m 2 -r 127.0.0.1:4532 f              # RX frequency (Hz)
rigctl -m 2 -r 127.0.0.1:4532 F 147030000    # QSY (must be in the channel plan)
rigctl -m 2 -r 127.0.0.1:4532 r o c          # shift, offset (Hz), TX CTCSS (0.1 Hz)
rigctl -m 2 -r 127.0.0.1:4532 U TSQL 1       # move to the tone-squelch variant
rigctl -m 2 -r 127.0.0.1:4532 E 4            # select Remote Channel 4 directly
rigctl -m 2 -r 127.0.0.1:4532 l STRENGTH     # dB relative to S9 (from RSSI)
rigctl -m 2 -r 127.0.0.1:4532 \get_dcd       # 1 = channel busy (SQ)

# interactive
rigctl -m 2 -r 127.0.0.1:4532
```

Other programs: in flrig, WSJT-X, GridTracker, Gpredict, CQRLOG, etc.,
pick **"Hamlib NET rigctl"** and enter `127.0.0.1:4532` (or the node's
LAN address if you set `listen = "0.0.0.0"` in `tkx90.toml`).

Python (Hamlib bindings):

```python
import Hamlib
rig = Hamlib.Rig(Hamlib.RIG_MODEL_NETRIGCTL)
rig.set_conf("rig_pathname", "127.0.0.1:4532")
rig.open()
rig.set_freq(Hamlib.RIG_VFO_CURR, 146940000)
print(rig.get_freq(), rig.get_rptr_shift(), rig.get_ctcss_tone())
```

## What works

| rigctl | Supported | Notes |
|---|---|---|
| `f` / `F` | yes | `F` picks the best Remote Channel with that RX frequency. Its shift and tones come with it. |
| `m` / `M` | FM only | `M FM 0` succeeds, anything else gives `RPRT -1`. |
| `r` / `R` | yes | Changes channel to the one with the requested shift on the same RX frequency. |
| `o` / `O` | yes | Same as `R`. If you are on a simplex channel, the offset is kept for the next `R`. |
| `c` / `C` | yes | TX CTCSS in tenths of Hz, e.g. `C 1000`. |
| `\get_ctcss_sql` / `\set_ctcss_sql` | yes | RX tone squelch. |
| `u`/`U TONE`, `u`/`U TSQL` | yes | |
| `e` / `E` | yes | Memory number = Remote Channel number (1–15). |
| `l STRENGTH`, `l RAWSTR` | yes | Raw = DB25-1 RSI in mV. Calibrate `[rssi]` in tkx90.toml. |
| `\get_dcd` | yes | SQ output. |
| `t` | yes | TXS output (radio is transmitting). |
| `T` | **no** (`RPRT -11`) | app_rpt and the URI own PTT. |
| `V`, `v`, `s` | dummy | Single VFO, no split. |

All set commands return `RPRT -1` when no programmed channel satisfies
them. Set `read_only = true` in `[hamlib]` to stop Hamlib clients from
changing the channel and leave that to app_rpt.

Hamlib caches the frequency on the client side. After an `E` (memory
select), an `f` in the *same* `rigctl` invocation can show the old value.
A new connection reads the real one.

## Why not a native Hamlib backend?

The radio itself has no CAT protocol for Hamlib to drive. Everything
`tkx90-catd` does is channel selection through the accessory port, plus
reading status lines. Writing a Hamlib backend for the Pico's protocol
would also be possible (`../firmware/PROTOCOL.md`). The daemon can't be
dropped either way, because app_rpt needs the Kenwood dialect and only
one process can own the Pico's serial port.

Tested against Hamlib 4.5.5 `rigctl -m 2` (open, `\chk_vfo`, `\dump_state`,
get/set freq, shift, offset, CTCSS, TSQL, memory, level, PTT refusal).
