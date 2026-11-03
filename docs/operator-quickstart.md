# User Quickstart

This guide walks a field operator through a passive RDS time-sync
session with `rdsclock`. It assumes a Linux host with an RTL-SDR
USB dongle and an FM-band antenna. For a short presentation with a synthetic
fallback, use the [demo walkthrough](demo.md). Read the
[hardware validation report](hardware-validation-2026-10-31.md) before making
claims about clock accuracy.

## 1. Pre-deployment Checklist

- [ ] `python3 --version` is 3.12 or newer.
- [ ] For offline operation, install dependencies beforehand and use a
      local receiver or an isolated receiver network. Live mode opens a
      TCP connection to the configured `rtl_tcp` endpoint.
- [ ] `rtl_tcp` is installed. On Debian/Ubuntu: `apt install rtl-sdr`.
- [ ] An FM antenna is connected to the RTL-SDR.
- [ ] The receiver is available exclusively; close other `rtl_tcp` clients.
- [ ] Compare gain settings for the selected station using clean groups/s
      and full-scale I/Q occupancy. A setting suitable for one station can
      lose another station entirely.

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

First locate RDS stations on your antenna. This broad scan is slow and a
three-second dwell can miss Clock-Time:

```bash
rdsclock scan --start 87.5 --end 108 --step 0.2 --duration 3
```

A line marked `[CT]` means a valid Clock-Time declaration was observed. It
does not establish correct UTC or source authenticity. An RDS station without
`[CT]` in this short scan may still broadcast 4A. Revisit a selected audible
frequency for at least 70–130 seconds, and compare the declared time with an
independent reference when one is available:

```bash
rdsclock live --freq 102.4 --gain 8.7 --duration 75 --settle-seconds 1 -v
```

The frequency and gain are examples from one measured setup. Before each
recording, incoming IQ is actively consumed and discarded during settling and
until the bounded drain finds a quiet client queue. The full requested
`--duration` is then captured. RTL-TCP has no tuning acknowledgement or sample
timestamps; an empty client queue does not prove freshness of a delayed remote
buffer. The program reports a drain error if its boundary cannot be established.

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
| `HIGH`   | At least three distinct confirmed PIs agree, with recent CT and small model uncertainty. |
| `MEDIUM` | At least two distinct confirmed PIs agree within the freshness policy. |
| `LOW`    | Fewer confirmed identities, older observations, or weaker model confidence. |
| `STALE`  | No usable estimate within the freshness or configured uncertainty bounds. |

These labels describe agreement under the model. Related observations are
deduplicated by channel and known PI. Different PI codes can still share a
clock source or a common error; `HIGH` does not authenticate transmissions or
verify absolute UTC. The displayed uncertainty is a model estimate, not a
calibrated error bound. Receipt timestamps are host estimates, and their
accuracy includes uncharacterized receiver and network delays.

### Machine-readable output

Add `--jsonl` to write one JSON object per line to stdout, for another
program or a log file. The operator display moves to stderr:

```bash
rdsclock recon --start 87.5 --end 108 --max-stations 5 --jsonl > recon.jsonl
```

Each record starts with `event`, `schema` and `host_utc`, the host clock
when the record was written, which is not a verified time. In loop order,
the events are `recon_started`, `acquisition_started`, `scan_step`,
`watchlist_updated`, `maintenance_started`, `hop_result`, `cycle_completed`
and `recon_stopped`. `cycle_completed` holds the data of the status block,
including the consensus and its trust level. Times are ISO 8601, and
unavailable values such as an infinite uncertainty are `null`. A stream that
ends without `recon_stopped` was stopped by an error; check stderr and the
exit status. `--jsonl` covers live reception only, not `--from-dir`.

## 6. Adjusting the Wrist Watch

Before using the display to set another clock, establish that the received
UTC is suitable for the task. A `HIGH` label alone is insufficient: the
hardware validation recovered clean CT declarations roughly an hour away
from host UTC. If an independent reference confirms the estimate:

1. Read the `CONSENSUS:` line.
2. Wait for a fresh cycle (the `age` counter goes back to ~0).
3. Set the watch to the displayed UTC. Add or subtract the
   local-time offset as appropriate; the LTO is shown next to the
   `ClockTime` line in `decode` output.

## 7. Failure Modes

| Symptom                             | Probable cause                                              | Fix                                                          |
|-------------------------------------|-------------------------------------------------------------|--------------------------------------------------------------|
| `scan` finds no stations | Antenna, gain or local reception conditions | Check cabling and compare AGC with several manual settings; use clean groups/s and I/Q endpoints. |
| RDS groups appear, no `[CT]` | Capture missed 4A, weak reception, or no usable CT | Observe the selected station for 70–130 s; absence in a short capture is inconclusive. |
| Many full-scale I/Q values | Saturation somewhere in the received signal path | Compare lower manual gain while preserving clean group recovery; `Δf` is not currently an RF CFO measurement. |
| `trust=STALE` from start            | All visible stations stopped CT during the run              | Increase `--max-stations`; manual cross-check.               |
| `OUTLIERS:` appears | Source disagreement, source identity problems or timing error | Inspect the individual PI, CT and receipt estimates; disagreement alone does not establish spoofing. |
| Header timeout or connection reset | Another client, unavailable server or interrupted service | Check exclusive access and server logs; reconnect after resolving the cause. |
| Drain timeout | The client could not establish a quiet boundary | Resolve backlog or throughput limits and reconnect; do not relabel stale samples as the new station. |

## 8. Shutdown

Stop the receiver with `Ctrl+C`. If you saved captures with
`--save`, treat the `*.iq` files as sensitive — they identify the
capture frequencies and timing.

```bash
make clean           # removes .venv, build/ and pytest caches
```
