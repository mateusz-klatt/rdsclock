> 🇵🇱 Polska wersja: [`README.pl.md`](README.pl.md)

# rdsclock — Passive RDS Clock-Time Receiver

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](pyproject.toml)
[![Tests](https://github.com/mateusz-klatt/rdsclock/actions/workflows/test.yml/badge.svg)](https://github.com/mateusz-klatt/rdsclock/actions/workflows/test.yml)
[![Sonar Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=mateusz-klatt_rdsclock&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=mateusz-klatt_rdsclock)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=mateusz-klatt_rdsclock&metric=coverage)](https://sonarcloud.io/component_measures?id=mateusz-klatt_rdsclock&metric=coverage)

A pure-Python passive receiver for the RDS (Radio Data System) Clock-Time
data stream broadcast by FM radio stations. Built for **GPS-denied,
NTP-unavailable** scenarios where an operator needs a non-GPS source of
UTC and cannot emit RF.

The package is composed of small, audit-friendly modules: DSP primitives,
the RDS block / group layers, a synthetic-signal generator, a multi-source
time-consensus engine, and a continuous "recon" mode that hops across
multiple FM stations and aggregates their clocks into a single, robust
time estimate with an explicit uncertainty.

## Quick Start

Passive receiver workflow:

```bash
# 1. Start rtl_tcp on the host that has the dongle
rtl_tcp -a 127.0.0.1 -p 1234

# 2. Scan the band for stations broadcasting RDS Clock-Time (~17 min for the
#    full FM band at 30 s per channel — long enough to catch most PS rotations).
rdsclock scan --start 87.5 --end 108.0 --step 0.1 --duration 30

# 3. Continuous passive time consensus across an explicit set of known
#    CT-broadcasting stations. Use HOP mode when the stations are spread
#    further than ~2 MHz apart (any single-dongle FM band picks of public
#    radio fit this case).
rdsclock recon --start 87.5 --end 108.0 --step 0.1 --dwell 60 --iterations 3

# 4. WIDE mode — three stations decoded synchronously from one capture.
#    Requires that all frequencies fit within fs (2.4 MS/s default → ~2 MHz
#    span). Two CT-broadcasting stations + one bonus station fit this slot
#    in Warsaw: Polskie Radio Jedynka 102.4 + Radio Kolor 103.0 + Rock
#    Radio 103.7 (centre 103.05 MHz, span 1.3 MHz).
rdsclock multi --freqs 102.4,103.0,103.7 --mode wide --fs 2400000 \
  --duration 60 --save eter/wide-103.iq
# Each 60 s of 2.4 MS/s complex64 capture is ~2 GB in memory. Keep
# duration modest if your host is RAM-constrained — recon mode is
# the right tool for long-running observations.

# 5. HOP mode — multi-station baseline that does NOT need them adjacent.
#    Useful for the strongest CT broadcasters in Warsaw:
rdsclock multi --freqs 91.0,94.0,96.5,98.3,98.8,102.4,103.7,107.5 \
  --mode hop --duration 60 --save eter/hop-warsaw.iq
```

> **Known CT-broadcasting stations in Warsaw (verified May 2026):** 91.0 RMF FM,
> 94.0 Meloradio, 96.5 Radio Plus, 98.3 RMF Classic, 98.8 PR Trójka, 102.4 PR
> Jedynka, 103.7 Rock Radio, 107.5 Radio ZET. Other stations broadcast RDS PS
> but not Group 4A (commercial / religious stations frequently omit CT).

Python API:

```python
from rdsclock.decoder import decode_file
from rdsclock.time_consensus import TimeConsensus

# Offline: decode a captured IQ file
result = decode_file("eter/baseline-20260518-035918/live-102.4MHz-300s.iq", fs=250_000)
print(f"PI {result.info.pi:#06x}  PS {result.info.ps_name!r}")
print(f"Clock times observed: {len(result.info.clock_times)}")
for ct in result.info.clock_times:
    print(f"  {ct}  rx={ct.rx_monotonic_ns} ns")

# Sub-second consensus (requires multiple stations with rx_monotonic_ns set)
tc = TimeConsensus()
# … feed observations from multiple stations …
est = tc.sub_second_consensus()
if est is not None:
    print(f"Consensus UTC: {est.utc} (±{est.precision_ms:.0f} ms across {est.station_count} stations)")
```

Optional dependency groups (install with `pip install 'rdsclock[audio]'` etc.):

| Extra    | Brings in     | Enables                       |
|----------|---------------|-------------------------------|
| `audio`  | `sounddevice` | live FM audio playback        |
| `plot`   | `matplotlib`  | MPX spectrum and waterfall PNGs |
| `dev`    | pytest, ruff  | running the test/lint suite   |

## Architecture

```mermaid
flowchart TB
    subgraph pkg[src/rdsclock/]
        cli[cli.py<br/>CLI entry point]
        decoder[decoder.py<br/>IQ → groups → StationInfo]
        recon[recon.py<br/>continuous passive receiver]
        synth[synth.py<br/>synthetic IQ generator]
        channelizer[channelizer.py<br/>wide-band → N narrow]
        consensus[time_consensus.py<br/>multi-source consensus]
        dsp[dsp.py<br/>DSP primitives]
        rds_blocks[rds_blocks.py<br/>block layer + CRC]
        rds_clock[rds_clock.py<br/>MJD ↔ datetime · 4A]
        rds_groups[rds_groups.py<br/>PS / RT / CT]
        rtl_tcp[rtl_tcp.py<br/>rtl_tcp client]
        audio[audio.py<br/>FM audio<br/>optional]
        plot[plot.py<br/>spectrum / waterfall<br/>optional]
    end
    tests[tests/<br/>≈ 140 unit & integration tests]
    docs[docs/<br/>architecture · threat model · quickstart]

    cli --> decoder
    cli --> recon
    cli --> synth
    cli --> audio
    cli --> plot
    decoder --> dsp
    decoder --> rds_blocks
    decoder --> rds_groups
    recon --> decoder
    recon --> rtl_tcp
    recon --> consensus
    synth --> rds_blocks
    synth --> dsp
    channelizer --> dsp
    channelizer --> decoder
    rds_groups --> rds_clock
    rds_groups --> rds_blocks
```

The tree itself lives under `src/rdsclock/`; see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the data-flow
description of each module.

## What FM MPX Looks Like

| Synthetic capture | Real local capture |
|-------------------|--------------------|
| ![Synthetic FM MPX spectrum](docs/images/mpx_spectrum_synthetic.png) | ![Real FM MPX spectrum](docs/images/mpx_spectrum_real.png) |
| Generated by `rdsclock.synth.synthesize_fm_iq` (no broadcast content). | Spectrum derived from a local FM capture. The plot is a derivative work; the source IQ is not redistributed. |

The two plots show the FM-demodulated baseband: on the left an entirely
synthetic stream produced by the package, on the right a real-world
capture analysed by the same `rdsclock plot` renderer. The annotated
bands are the four standard FM-MPX components:

| Band            | Component                                |
|-----------------|------------------------------------------|
| 0 – 15 kHz      | Mono audio (L+R)                         |
| 19 kHz          | Stereo pilot tone                        |
| 23 – 53 kHz     | Stereo (L–R) double-sideband subcarrier  |
| 57 kHz (±~2 kHz)| RDS BPSK on the 3rd harmonic of the pilot |

The receiver locks the RDS subcarrier through the pilot
(`estimate_pilot_19khz × 3`), which is robust against the typical
RTL-SDR ppm drift. Reproduce either figure with:

```bash
# Synthetic
rdsclock generate build/test.iq --snr 30
rdsclock plot build/test.iq --out spectrum.png      # needs the [plot] extra

# From your own local capture (no broadcast content shared, plot only).
# Pick any strong local FM frequency — the frequency below is illustrative,
# not a station recommendation. Public-radio frequencies generally broadcast
# RDS Clock-Time more reliably than commercial / religious channels.
rdsclock live --freq 102.4 --duration 30 --save build/local.iq
rdsclock plot build/local.iq --out spectrum.png
```

## RDS Group 4A — Bit Layout (IEC 62106-2:2021, Figure 11)

The 34-bit Clock-Time payload (MJD 17 + Hour 5 + Minute 6 + LTO sign 1 +
LTO magnitude 5) is distributed across data words B, C and D
(MSB-first per word):

| Field      | Block B       | Block C        | Block D         |
|------------|---------------|----------------|-----------------|
| MJD        | `[1:0]` (MSB) | `[15:1]` (LSB) | —               |
| HOUR       | —             | `[0]` (MSB)    | `[15:12]` (LSB) |
| MINUTE     | —             | —              | `[11:6]`        |
| LTO sign   | —             | —              | `[5]`           |
| LTO mag    | —             | —              | `[4:0]`         |

The equivalent bit-extraction formula is:

```c
MJD    = ((B & 0x0003) << 15) | (C >> 1)
HOUR   = ((C & 0x1) << 4)     | (D >> 12)
MINUTE = (D >> 6) & 0x3F
SIGN   = (D >> 5) & 0x1
MAG    = D & 0x1F
```

The Modified Julian Day epoch is **1858-11-17 00:00 UT**. The
`hh:mm` field is UTC; local time = UTC + sign · magnitude · 30 min.

> The same bit layout is described in the free public
> [NRSC-4-B](https://nrscstandards.org/standards-and-guidelines/)
> standard, which is a superset of IEC 62106. See
> [`docs/REFERENCES.md`](docs/REFERENCES.md) for the full source list.

## Passive Time-Receiver Mode (`recon`)

`recon` implements a continuous, fully passive time receiver designed
for environments where:

- **GPS** may be jammed or spoofed,
- **NTP** is unavailable (no internet),
- the operator **cannot emit RF** of any kind,
- the broadcast language is **unknown** (so PS/RT are not used).

### Operating Principle

1. **Acquisition** — a quick band scan locates strong FM stations and
   tests each one for valid RDS Group 4A (Clock-Time).
2. **Maintenance** — the watchlist is hopped through, collecting one
   CT observation per station per cycle.
3. **Consensus** — the multi-source median of the observed times
   becomes the operator's UTC reference. Each station carries a
   **trust score** that decays when it diverges from the median
   (Hampel outlier rule).
4. **RF fingerprinting** — per-station features (CFO, RSSI, PI) are
   recorded for later analysis. Automated shift detection is on the
   roadmap.
5. **Receive timestamping** — live captures anchor decoded Group 4A
   receipt to the host monotonic clock and correct the bit-rate estimate
   from the 19 kHz pilot tone. With healthy NTP this is a roughly
   30-80 ms UTC claim; without internet, a 3+ station field demo should
   be treated as roughly 100-250 ms. Hardware-grade timing requires a
   hardware time source.
6. **Holdover** — between Clock-Time messages the receiver
   extrapolates UTC from a local monotonic clock disciplined by an
   estimated ppm drift. Uncertainty grows linearly with the age of
   the most recent CT.
7. **Operator display** — `UTC 2026-05-17 04:23:18  ±2s  N=3  trust=HIGH`.

See `docs/operator-quickstart.md` for an operator-oriented walkthrough
and `docs/THREAT_MODEL.md` for the security threat model.

## Hardware

- **RTL-SDR** USB dongle (tested: RTL2838 with R820T2 tuner).
- `rtl_tcp -a 127.0.0.1` (loopback only; do **not** expose to the
  network without a firewall — the protocol has no authentication).
- An FM-band (VHF II ~88–108 MHz) antenna.

## Testing

```bash
make test           # all tests, including real-recording validation
make test-fast      # skip 'slow' and 'real_sdr' tests
make coverage       # generate htmlcov/
```

The synthetic round-trip (`tests/test_decoder_synthetic.py`) is the
load-bearing correctness check: it generates IQ from a known clock,
runs the full pipeline, and asserts the decoded clock matches.

Real-world validation against a live RTL-SDR (`tests/test_real_recordings.py`)
is gated by the `real_sdr` marker and skips automatically when no
`rtl_tcp` daemon is reachable. These tests do not assume any specific
station or date — they verify pipeline invariants on whatever signal
the local antenna picks up.

## Status

- **1.0.0** — first stable release. The top-level public API is frozen
  at `from rdsclock import ...`; internal module helpers may still change.
- Real-IQ regression coverage and `tools/benchmark.py --check` provide a
  release gate against group-count, PI-code, and decode-time regressions.
- Multi-station sub-second consensus is available through
  `TimeConsensus.sub_second_consensus()` when observations carry
  `rx_monotonic_ns` receive timestamps.
- Precision claims are intentionally bounded: see the
  [0.4.0 changelog entry](CHANGELOG.md#040--2026-05-18) for the receive
  timestamping model and expected UTC precision ranges.
- 240+ tests including real-IQ regression coverage; line coverage
  **100 %** (tracked by SonarCloud).

## Legal Note

The receiver is passive — it never transmits and never emits RF, so
*receiving* broadcast FM is lawful in Poland and across the EU.
However, **the captured IQ data and any decoded audio should be kept
local**:

- Polish *Prawo komunikacji elektronicznej* (Dz.U. 2024 poz. 1221)
  preserves the confidentiality of electronic communications and
  restricts dissemination of received content.
- The audio embedded in an FM capture is generally a copyrighted work.

Local recording for technical or amateur-radio use is widely accepted;
public redistribution of those recordings typically requires
permission from the rights holders. The synthetic generator shipped
with the package produces copyright-free IQ files for tests and
demos. See [`docs/datasets.md`](docs/datasets.md) for details and
[`SECURITY.md`](SECURITY.md) for the security checklist. **None of
the above is legal advice.**

## Licence

[Apache License, Version 2.0](LICENSE).
