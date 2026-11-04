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

### HOP: a fixed list of stations

`--hop` skips the band scan and hops between the stations you list, for
example three stations known to broadcast Clock-Time:

```bash
rdsclock recon --hop 98.8,102.4,107.5 --gain 28 --dwell 75
```

An acquisition then checks only these stations, for `--scan-dwell` seconds
each, and the watchlist keeps every one with RDS, with or without Clock-Time in
that short check; `--rssi-threshold` and `--max-stations` do not apply. HOP
reaches stations anywhere in the band but records them one after another: a
cycle of three stations takes three dwells.

### WIDE: several stations at once

When the stations fit inside the receiver bandwidth, `--wide` records them
together instead of scanning the band and hopping between them:

```bash
rdsclock recon --wide 102.0,102.4,103.0 --center 102.85 --gain 28 --dwell 75
```

Every cycle is then one capture of `--dwell` seconds at 2.4 MS/s (`--fs`)
around `--center`. The channelizer separates the stations, so all of them see
the same minute edge, and a cycle of three stations takes one dwell instead of
three. An acquisition is a capture of `--scan-dwell` seconds that decodes the
listed stations; the watchlist keeps every station with RDS. Without
`--center`, `recon` places the centre as near the middle as it can while every
station stays 150 kHz from the receiver's DC offset at the centre and 300 kHz
from the mirror image of every other station, which I/Q imbalance reflects
about the centre. When the stations leave no such room, it keeps only the DC
clearance, and failing that takes the middle; halfway between two stations,
each would sit on the other's image.
The stations need their span plus
a 200 kHz guard band on each side, and `recon` refuses a list that does not
fit.

A 75-second capture at 2.4 MS/s takes about 1.4 GB, and decoding one station
at a time adds as much again: plan for about 5 GB of memory. `rtl_tcp` sends
4.8 MB/s, so a remote receiver needs a link that sustains it. Set the gain
explicitly, since the stations share one 8-bit converter. `--start`, `--end`,
`--step`, `--rssi-threshold` and `--max-stations` apply to band scans only.

### Terminal dashboard

Install the optional extra (`pip install 'rdsclock[tui]'`, or `--extra tui` with
`uv sync`) and add `--tui` to the same command:

```bash
rdsclock recon --start 87.5 --end 108 --max-stations 5 --tui
```

The dashboard starts `recon --jsonl` as a child process with the same receiver
settings and shows:

- the phase, with scan or hop progress (with `--wide`, the capture centre) and
  the time to the next scheduled rescan;
- the consensus, advanced every second between cycles, with the current host
  clock difference, outliers, the time since the receiver assessed its trust
  level and the same uncertainty caveat as the text display. Once the median CT
  age passes the consensus freshness limit (300 s), for example during a long
  rescan, the estimate is shown as expired;
- one row per watchlist station: PS, PI (unknown until the hop confirms it),
  RSSI, groups and corrected groups, the latest CT received there and its LTO,
  track age, trust score and consensus status;
- a log of scans, hops and cycles.

`q` stops the receiver as `Ctrl+C` would and quits; a receiver that has not
stopped after 5 seconds is killed. `r` sends `SIGUSR1`, which starts an
acquisition scan after the current cycle; live `recon` without the dashboard
reacts to `kill -USR1 <pid>` the same way, and the dashboard passes on a
`SIGUSR1` sent to its own process. `p` pauses the view while reception
continues. Station names are shown as plain printable text. If the receiver
fails, its last error output stays in the log and the dashboard exits with
status 1.

### Browser dashboard

`--web` serves the same view to a browser and needs no extra dependencies:

```bash
rdsclock recon --start 87.5 --end 108 --max-stations 5 --web
```

Open <http://127.0.0.1:8080/>; `--web-port` changes the port. The page has
*Rescan* and *Pause view* buttons, and `Ctrl+C` in the terminal stops the
receiver and the server. The dashboard has no authentication and listens on
loopback only. To watch a remote receiver, forward the port instead of exposing
it:

```bash
ssh -N -L 8080:127.0.0.1:8080 SDR_HOST
```

`--web-host` can bind another address. The dashboard then warns that anyone who
can reach that address can watch it, and it still accepts rescans only from the
receiver's own machine. It refuses requests that name an unknown host, which
blocks DNS rebinding. A rescan needs a request from the page itself, so other
web pages cannot trigger one.

### Desktop window

`--gui` shows the same view in a desktop window, using the tkinter module that
ships with most Python builds (on Debian/Ubuntu: `apt install python3-tk`):

```bash
rdsclock recon --start 87.5 --end 108 --max-stations 5 --gui
```

The window has *Rescan*, *Pause view* and *Quit* buttons, also on the keys `r`,
`p` and `q`. Closing the window or pressing `Ctrl+C` in the terminal stops the
receiver as `Ctrl+C` would, and a `SIGUSR1` sent to the window's process requests
a rescan. Without a display (for example over plain SSH) the command explains
that it cannot open a window; use `--tui` or `--web` there.

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

With `--hop`, `acquisition_started` reports the span of the listed stations
with a `scan_step_mhz` of 0, and `n_points` counts them (`null` for a band
scan). With `--wide`, `acquisition_started` and `maintenance_started` also
carry `wide_center_hz` and `wide_sample_rate`, which are `null` for band scans
and hops. A WIDE acquisition reports the span of the listed stations with a
`scan_step_mhz` of 0, followed by one `scan_step` per station, and every
station of a WIDE capture produces a `hop_result`.

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
