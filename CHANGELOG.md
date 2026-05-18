# Changelog

All notable changes to `rdsclock` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] — 2026-05-18

### Added

- Decoded Clock-Time values can now carry receive timestamp metadata.
  `ClockTime` has optional `rx_monotonic_ns` and `tx_latency_ns` fields,
  and live/recon captures anchor Group 4A receipt on the host monotonic
  clock when a capture start is known.
- The block scanner now has `find_groups_in_bitstream_with_positions()`
  for callers that need each decoded group's bitstream start position;
  the existing `find_groups_in_bitstream()` API remains unchanged.
- Pilot-derived bit-rate drift measurement estimates the RDS bit rate
  from the 19 kHz stereo pilot and falls back to the nominal 1187.5 bit/s
  rate when the pilot is unstable.
- `TimeConsensus.sub_second_consensus()` adds a parallel sub-second path
  that learns per-station Group 4A transmit latency and returns a
  timestamp consensus when at least two stations have learned latencies.

### Changed

- The decoder threads group bit positions through `DecodeResult` and can
  attach `rx_monotonic_ns` to decoded Clock-Time entries without changing
  group counts, PI codes, or the 0.3.0 syndrome-correction behavior.
- Documentation now states the precision floor honestly: roughly
  30-80 ms in an NTP-healthy lab setup, and about 100-250 ms for a
  field demo with 3+ stations. Hardware-grade timing still requires a
  hardware time source.

## [0.3.0] — 2026-05-18

### Added

- RDS block sync can now tolerate one single-bit block error per group
  using the block syndrome table from the `(26, 16)` RDS CRC code.
  Decoder runs recover the corrected dataword when the flipped bit is
  in the 16-bit data half; CRC-half errors are accepted without changing
  the dataword.
- `DecodeResult` now reports `n_groups_clean` and
  `n_groups_corrected` alongside the existing total `n_groups`.

### Changed

- `decode_iq` enables conservative single-bit correction by default.
  `find_groups_in_bitstream` remains strict unless callers explicitly
  pass `tolerate_single_bit=True`.
- Existing decode runs may now report more groups, especially on weak
  stations. This is the intended behavior; group counts are not directly
  comparable to 0.2.x outputs.

## [0.2.2] — 2026-05-18

### Fixed

- FM audio extraction now supports rational SDR-to-audio sample-rate
  ratios. The common rtl_tcp path of 250 kS/s input to 48 kHz audio
  uses polyphase resampling after the existing audio low-pass stage,
  while integer ratios keep the fast FIR decimation path.
- Programme Service decoding now validates completed PS frames before
  exposing them through `StationInfo.ps_name`. Dynamic-PS stations that
  scroll text through Group 0A/0B no longer overwrite the displayed PS
  with mixed fragments from different rotations.

### Changed

- `StationInfo.ps_name` and CLI displays may remain empty for the first
  couple of PS rotations, and dynamic-PS stations may stay empty unless
  the same 8-character frame is seen twice consecutively. Use
  `StationInfo.latest_ps_candidate` for the latest complete unvalidated
  PS frame.

## [0.2.1] — 2026-05-18

### Changed

- Decoder hot path optimised: ~7× faster end-to-end on the baseline
  IQ corpus. A 5-minute real-broadcast capture now decodes in
  ~45-50 s instead of ~300-380 s — fast enough for multi-station
  real-time consensus on a single thread. Group counts and PI codes
  are **bit-identical** to 0.2.0; this is performance only.
- `rds_blocks.crc10` is now table-driven (1024-entry lookup,
  precomputed at module load) and bit-exact against the previous
  shift-register implementation over the full `0..65535` input space.
- `rds_blocks.bits_to_word` and `rds_blocks.find_groups_in_bitstream`
  use NumPy vectorisation (`sliding_window_view` + batched dot
  product) instead of per-window Python loops. The whole-stream
  group scan is now `O(n)` numpy ops with a single Python pass to
  pick the non-overlapping group positions.
- `dsp.costas_loop_bpsk` accepts an optional Numba JIT fast path
  (installed via the new `[fast]` extra). Falls back to the pure
  Python loop when Numba is unavailable — Numba is not a hard
  dependency.

### Added

- `tools/benchmark.py` — regression benchmark that decodes every
  IQ in a baseline directory and writes machine-readable JSON.
  Use to compare decoder changes head-to-head against a fixed
  corpus.
- `[fast]` optional extra in `pyproject.toml` (currently brings
  `numba` for the JIT Costas path).

## [0.2.0] — 2026-05-17

This is the first release that **actually decodes real FM broadcasts**.
Releases 0.1.0 – 0.1.2 worked only on the package's own synthetic IQ,
because the synth and decoder both spoke NRZ to each other while real
RDS uses biphase / Manchester coding. The bug was discovered during a
multi-station live test in Warsaw against five known transmitters
(Polskie Radio Trójka 98.8 Raszyn, RMF FM 91.0 Raszyn, Polskie Radio
Jedynka 102.4 Raszyn, Radio Plus 96.5 PKIN, Polskie Radio RDC 101.0
PKIN) — all returning 0 groups before this release. After the fix all
five decode cleanly with correct Polish PI codes (0x32xx for public
broadcast, 0x3F44 for RMF FM).

The version bump from 0.1.x to 0.2.0 reflects observable API changes
in `rdsclock.synth` and `rdsclock.dsp` (see *Breaking* below).

### Fixed

- **Decoder now works on real FM broadcasts.** `synth.py` previously
  emitted NRZ while the decoder consumed the same shape — synthetic
  round-trips passed but real broadcasts always returned zero groups.
  Synth now emits proper biphase chips and the decoder runs a biphase
  matched filter with automatic symbol-offset selection.
- Removed `coarse_freq_correction` from the decoder pipeline. Its
  phase-difference estimator behaves like noise for weak real RDS,
  which Costas can absorb cleanly on its own once AGC is in place
  (3× more groups recovered in the Warsaw A/B test).

### Changed (breaking)

- `rdsclock.synth.biphase_symbols(bits, samples_per_bit)` now emits
  two chips per bit (length = `2 * len(bits) * (samples_per_bit // 2)`).
  Previously it emitted one NRZ sample per bit. Callers depending on
  the old output length must adjust.
- `rdsclock.synth.rds_baseband` requires `fs / symbol_rate` to be an
  even integer so each biphase chip has equal duration.
- `rdsclock.dsp.bits_from_symbols_diff` now expects the biphase
  matched-filter output sampled once per bit. Its hard-decision logic
  is unchanged; its input semantics are.
- Default `costas_loop_bpsk(alpha, beta)` raised from `(0.1, 0.002)`
  to `(0.3, 0.005)` for faster lock on weak real-broadcast BPSK.

### Added

- `rdsclock.dsp.agc()` — mean-magnitude normaliser, applied before
  the Costas loop in `decode_iq`.
- `rdsclock.dsp.biphase_matched_filter()` — matched filter for the
  biphase pulse shape (`[+1]*sps_half + [-1]*sps_half`).
- Real-IQ regression test (`tests/test_real_iq_regression.py`)
  backed by an actual broadcast capture from Polskie Radio Trójka
  98.8 in Warsaw (6 s, 250 kS/s u8 IQ, ~3 MB in
  `tests/fixtures/`). The decoder must recover at least 10 groups
  and a PI in the Polskie Radio range. This test exists so the
  NRZ-vs-biphase mismatch can never silently return.

## [0.1.2] — 2026-05-17

### Added

- `docs/REFERENCES.md` — curated list of public sources used to
  build and verify the decoder: IEC 62106-2:2021, the freely
  downloadable NRSC-4-B RBDS standard (a public superset of
  IEC 62106), the open-source `redsea` decoder by Oona Räisänen,
  the Wikipedia RDS overview, and the relevant RTL-SDR / osmocom
  documentation. A note in the README's Group 4A section now
  points readers at the free NRSC-4-B PDF when they don't have
  IEC webstore access.

### Changed

- Refreshed the README "Status" section: 226 tests, **100 % line
  coverage**, current version pinned to 0.1.2.

## [0.1.1] — 2026-05-17

### Changed

- Packaging metadata now uses PEP 639 SPDX format
  (`license = "Apache-2.0"` + `license-files = ["LICENSE"]`)
  instead of the legacy `license = { file = "LICENSE" }`. PyPI now
  shows a short licence label instead of the full Apache text.
  Requires `setuptools>=77` at build time.
- Dropped the redundant `License :: OSI Approved :: Apache Software License`
  trove classifier (covered by the SPDX expression).

### Added

- Two new test files (`tests/test_coverage_extra.py`,
  `tests/test_coverage_edges.py`) plus a shared `fake_rtl_tcp`
  fixture in `tests/conftest.py`. **Line coverage is now 100%
  across every module** (219 unit and integration tests).

## [0.1.0] — 2026-05-17

### Added

- Initial public release of the RDS Clock-Time decoder package.
- `rdsclock.dsp` — DSP primitives: channel filter, FM demodulation,
  RDS subcarrier shift+filter, coarse frequency correction, symbol LPF,
  Costas loop (BPSK), best-symbol-offset selection,
  Mueller–Müller clock recovery, pilot-based RDS carrier estimation.
- `rdsclock.rds_blocks` — RDS block layer (16-bit data + 10-bit CRC),
  syndrome offset words A/B/C/C'/D, group extraction from bitstream,
  differential encode/decode, strict C/C' version validation.
- `rdsclock.rds_clock` — IEC 62106-2:2021 Figure 11 Group 4A layout
  (MJD across `B[1:0]` + `C[15:1]`, HOUR in `C[0]` + `D[15:12]`,
  MINUTE in `D[11:6]`, LTO sign in `D[5]`, LTO magnitude in `D[4:0]`).
- `rdsclock.rds_groups` — parser/encoder for PS (0A/0B), RT (2A/2B),
  Clock-Time (4A).
- `rdsclock.synth` — synthetic IQ generator: BPSK on 57 kHz subcarrier,
  19 kHz pilot, AWGN at configurable SNR. Round-trip validated.
- `rdsclock.decoder` — full IQ → groups → `StationInfo` pipeline.
- `rdsclock.channelizer` — wide-band capture → N narrow channels.
- `rdsclock.rtl_tcp` — minimal `rtl_tcp` client (context manager).
- `rdsclock.time_consensus` — multi-source CT consensus with
  per-station trust score, Hampel outlier detection, and a holdover
  discipline with ppm-drift uncertainty growth.
- `rdsclock.recon` — continuous passive time receiver (live + offline).
- `rdsclock.audio` — FM audio path: `fm_audio_from_iq()`, live
  `play_iq_live()` via rtl_tcp, and `play_iq_file()` for replays
  (optional, requires the `[audio]` extra).
- `rdsclock.plot` — annotated MPX spectrum (`mpx`) and IQ waterfall
  (`waterfall`) renderers; PNG output (optional, requires the `[plot]` extra).
- `rdsclock.cli` — `generate`, `decode`, `live`, `multi`, `demo`,
  `recon`, `scan`, `plot`, `play` sub-commands.
- ~140 unit and integration tests; line coverage above 80%.
- `make demo` — self-contained 3-station multi-channel demonstration.
- `make recon` / `make recon-offline` — passive recon mode (live and replayed).
- Optional dependency groups: `[audio]` (sounddevice), `[plot]` (matplotlib),
  `[dev]` (pytest, pytest-cov, ruff).

### Security

- Default `rtl_tcp` host is `localhost`. The README warns against
  exposing the daemon to non-loopback interfaces without a firewall.
- All operations are receive-only. No outbound network traffic except
  the local `rtl_tcp` socket.
- IQ recordings and scan logs treated as sensitive — see `SECURITY.md`.

## Release Notes Template (for future versions)

```
## [X.Y.Z] — YYYY-MM-DD

### Added       — new features
### Changed     — non-breaking modifications to existing behaviour
### Deprecated  — features scheduled for removal
### Removed     — features removed in this release
### Fixed       — bug fixes
### Security    — fixes for security issues
```
