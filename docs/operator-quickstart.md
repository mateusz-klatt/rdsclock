# User Quickstart

This guide walks a field operator through a passive RDS time-sync
session with `rdsclock`. It assumes a Linux host with an RTL-SDR
USB dongle and an FM-band antenna.

## 1. Pre-deployment Checklist

- [ ] `python3 --version` is 3.12 or newer.
- [ ] The host is **air-gapped** if you need offline operation. `rdsclock`
      itself opens no outbound sockets, but the dependency install
      step needs internet once.
- [ ] `rtl_tcp` is installed. On Debian/Ubuntu: `apt install rtl-sdr`.
- [ ] An FM antenna is connected to the RTL-SDR.

## 2. Bring Up the SDR

Run `rtl_tcp` bound to loopback only — never to `0.0.0.0`:

```bash
rtl_tcp -a 127.0.0.1 -p 1234
```

If the kernel claims the `dvb_usb_rtl28xxu` driver, blacklist it:

```bash
sudo tee /etc/modprobe.d/blacklist-rtl.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
EOF
sudo modprobe -r dvb_usb_rtl28xxu
```

## 3. Install rdsclock

```bash
git clone <repo-url> rdsclock
cd rdsclock
make setup            # creates .venv, installs the package
make test-fast        # confirms the suite passes without an SDR
```

## 4. First Run

Find a station with valid Clock-Time on your antenna:

```bash
rdsclock scan --start 87.5 --end 108 --step 0.2 --duration 3
```

Look for lines marked `[CT]`. If at least one appears, you have a
working CT source.

## 5. Continuous Time Sync (`recon` mode)

```bash
rdsclock recon \
    --start 87.5 --end 108 --max-stations 5 \
    --rescan-min 10 \
    --rssi-threshold -25
```

The display refreshes after each hop cycle. The key line is the
consensus block:

```
CONSENSUS: UTC 2026-05-17 04:23:18  ±2s  N=3  trust=HIGH  age=12s
SYSTEM:    2026-05-17 04:23:20Z  Δ=-2.0s
```

Trust levels and what they mean:

| Trust    | Meaning                                                              |
|----------|----------------------------------------------------------------------|
| `HIGH`   | ≥3 stations agree, fresh CT (< 2 min). Use without reservation.      |
| `MEDIUM` | 2 stations or a fresh single source. Cross-check with another method.|
| `LOW`    | 1 station or stale (> 5 min). Consider re-scanning.                  |
| `STALE`  | Holdover beyond the configured precision threshold. **Do not trust.** |

## 6. Adjusting the Wrist Watch

When trust is `HIGH` and `±X` is acceptable for your task:

1. Read the `CONSENSUS:` line.
2. Wait for a fresh cycle (the `age` counter goes back to ~0).
3. Set the watch to the displayed UTC. Add or subtract the
   local-time offset as appropriate; the LTO is shown next to the
   `ClockTime` line in `decode` output.

## 7. Failure Modes

| Symptom                             | Probable cause                                              | Fix                                                          |
|-------------------------------------|-------------------------------------------------------------|--------------------------------------------------------------|
| `scan` finds no stations            | Antenna disconnected, or band quiet                         | Verify cabling and tuner gain (`--gain 35`).                 |
| `scan` finds stations, no `[CT]`    | Local stations not broadcasting Group 4A, or weak SNR       | Hop to a different region or wait for a quieter time.        |
| Δf offset > 1 kHz consistently      | Tuner ppm drift                                             | Pass `--ppm <correction>` to `live` / `recon`.               |
| `trust=STALE` from start            | All visible stations stopped CT during the run              | Increase `--max-stations`; manual cross-check.               |
| `OUTLIERS:` line appears            | One station is wrong or attempting to spoof                 | Reduce its trust via repeated outlier hits, or remove freq.  |

## 8. Shutdown

Stop the receiver with `Ctrl+C`. If you saved captures with
`--save`, treat the `*.iq` files as sensitive — they identify the
capture frequencies and timing.

```bash
make clean           # removes .venv, build/ and pytest caches
```
