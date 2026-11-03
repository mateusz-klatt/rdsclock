# Demonstration and technical discussion

This sequence demonstrates FM RDS decoding, source attribution and conservative
time aggregation. It is suitable for a five-to-ten minute engineering discussion.
Keep synthetic examples and measurements from the antenna clearly identified.

## Prepare a checkout

Use Python 3.12 or newer and the locked dependencies:

```bash
uv sync --frozen --extra dev --extra plot --extra audio --no-install-project --no-build
export PYTHONPATH="$PWD/src"
uv run --no-sync --no-build python -m pytest tests/ -m 'not slow and not real_sdr' -q
```

The commands below assume that interpreter is `.venv/bin/python`. Synthetic
examples work without an SDR, audio output or network access after installation.

## 1. Known signal through the complete decoder

```bash
.venv/bin/python -m rdsclock demo --time 2026-10-31T12:00:00+00:00 --seed 42
```

Explain the path from complex IQ through FM demodulation, the 19 kHz stereo
pilot and 57 kHz RDS subcarrier, symbol recovery, block checkwords, station PI
and Group 4A fields. Expected time is known because this input is generated.
The result demonstrates algorithm correctness under the selected simulation,
not reception conditions or absolute clock accuracy.

## 2. Outlier and repeated-source behavior

```bash
.venv/bin/python examples/consensus_demo.py
```

Three distinct PIs agree within a few seconds; a fourth is ten minutes ahead.
A fifth frequency carries a repeated PI and must not increase the source count.
Expected result: three contributing sources and the 107.5 MHz outlier. These
are synthetic observations exercising the consensus API separately from DSP.

Discuss why CRC validity, source identity, independent clocks and authentic UTC
are different properties. `HIGH` reports agreement under the model; multiple
services can share a wrong upstream clock. The uncertainty estimate describes
model dispersion/holdover and is not a calibrated absolute error bound.

## 3. Live station changes

An exclusive `rtl_tcp` receiver and FM antenna are required. Start the server
on loopback on its host. For a remote receiver, use an SSH tunnel:

```bash
ssh -N -L 1234:127.0.0.1:1234 SDR_HOST
```

Choose audible stations at the demonstration location. These Warsaw frequencies
were tested in October 2026, but reception and gain are specific to the antenna:

```bash
.venv/bin/python -u -m rdsclock multi --host 127.0.0.1 \
  --freqs 102.4,98.8,107.5 --mode hop --duration 8 \
  --settle-seconds 1 --gain 19.7
```

Check distinct confirmed PIs, clean/corrected groups and reception quality.
Dynamic PS text can change while the PI remains stable. The receiver actively
consumes and discards buffered samples after a retune; it fails if it cannot
find a bounded capture boundary. RTL-TCP has no tune acknowledgement or hardware
timestamps, so the host estimate and channel attribution still have transport
limitations. The parser isolates fields by PI as a separate protection.

## 4. Received time and a reference clock

```bash
.venv/bin/python -u -m rdsclock live --host 127.0.0.1 \
  --freq 102.4 --gain 8.7 --duration 75 --settle-seconds 1 -v
```

An eight-second capture can miss Group 4A. Allow at least a minute when observing
CT, and report absence as an observation rather than proof of no transmission.
Compare the raw declared UTC and LTO with the host clock. The host itself must
be independently validated before using that comparison to assess accuracy.
Never silently subtract an hour to make broadcast time agree with a reference.

Our [hardware validation](hardware-validation-2026-10-31.md) records 19 captures
on eight frequencies, six confirmed PIs and hour-scale CT disagreement on several
services. It also shows why gain cannot be set globally: reducing saturation on
a strong station can remove RDS reception on a weak one. Choose gain using clean
groups per second, correction counts, endpoint occupancy and PI stability.

## Engineering tradeoffs to explain

- A single RTL-SDR hops between separated stations, so snapshots occur at
  different times. Wideband channelization is simultaneous within receiver
  bandwidth, at greater memory and processing cost.
- CPU work between captures can create old IQ queues. Draining and bounding
  acquisition are tested separately from demodulation correctness.
- Fields from different PI codes remain separate. A valid group from another
  station must not replace the selected station's clock or text.
- Consensus uses estimated reception time, rather than decoder completion,
  and deduplicates channels and confirmed PI values before assigning votes.
- A host-derived timestamp and a population median are not hardware timing,
  authenticated time or proof of independent sources.

Captured IQ and audio remain local. The checked-in hardware report contains
aggregated measurements, not captured broadcast content or recruitment records.
