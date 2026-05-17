# Changelog

All notable changes to `rdsclock` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
