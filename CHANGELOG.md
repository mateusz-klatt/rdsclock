# Changelog

All notable changes to `rdsclock` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.2.0] — 2026-11-05

### Added

- `recon --hop` items can name the `rtl_tcp` receiver that records them, as
  `HOST:PORT@MHZ` or `[IPv6]:PORT@MHZ`; items without one use `--host` and
  `--port`. Each receiver hops through its own stations in its own thread, in
  parallel with the others, so a cycle lasts as long as the busiest receiver
  instead of one dwell per station, and with one station per receiver every
  station can be heard for a full minute. A station listed twice for one
  receiver is an error, and `--wide` rejects receivers. One receiver works
  exactly as before.
- A receiver that fails is closed and its stations leave the watchlist while
  the others go on; it is connected again at the start of the next cycle and
  its stations are checked before they rejoin the hops. A receiver too slow to
  deliver a capture within its duration plus `max(15 s, 25 %)` of it fails
  the same way, so it cannot hold up the others. The run ends only when every
  receiver has failed. Ctrl+C stops every receiver at once, including one that
  is still connecting; a JSON Lines record being written is finished first.
- With several receivers, each one votes for the transmitter it last heard on a
  channel: receivers in different places that hear different transmitters on
  one frequency count as two sources, and the dashboards show each one's
  metrics in its own row.
- JSON Lines: `recon_started` gains `receivers` (`host:port`), `hop_result`,
  `scan_step` candidates and watchlist entries gain `receiver`, and a new
  `receiver_failed` event carries `iteration`, `receiver` and `error`. Each
  `cycle_completed` track gains `consensus_status` (`contributing`, `outlier`
  or `null`), since one frequency can carry a contributing and an outlying
  transmitter; with several receivers the dashboards take a row's status from
  it. Existing keys keep their meaning.
- The terminal, browser and desktop dashboards add a *Receiver* column when
  more than one receiver is configured, show in the status what each receiver
  is recording (for example
  `HOP · 1/2 stations recorded · 192.168.5.1:1234: done · 127.0.0.1:1235 → 98.80 MHz`),
  prefix hop log lines with the receiver, and log a receiver that fails and
  the first record that shows it working again. With one receiver they look
  as before.
- `tools/replay_rtl_tcp.py` serves a recorded IQ file (`cf32` or `cu8`) as an
  `rtl_tcp` receiver, a test aid for several receivers and the dashboards. Its
  Clock-Time is as old as the recording, so a live consensus treats it as an
  outlier; with two equal camps it reports no time. A `--max-queue-s` shorter
  than one transfer is refused.
- `recon --help` ends with examples, including several receivers and the replay
  tool.
- English, Polish and French texts for all of the above, a README section
  "Several receivers" in each language, the operator quickstart, the
  architecture notes on threads, the output queue and stopping, and release notes
  in all three languages.
## [2.1.0] — 2026-11-05

### Added

- `recon --ct-timezone [pi:HEX=|freq:MHZ=]ZONE` declares that a station sends
  local time in the Group 4A UTC field. IEC 62106 defines that field as UTC
  (local time = UTC + LTO), but Jedynka 102.4, Trójka 98.8, Radio ZET 107.5
  and RMF Classic 98.3, measured in Warsaw from 31 October to 5 November 2026,
  send CET there with an LTO of zero. The option is repeatable; a PI rule wins
  over a frequency rule, which wins over a bare zone, and `UTC` keeps the
  standard reading. The decoded hour and minute are converted from the zone's
  local time to UTC with the system time-zone database. A time in a repeated
  (`ambiguous`) or skipped (`nonexistent`) hour is reported but not used, and
  the LTO is kept but not applied. Without the option nothing changes.
- JSON Lines: `hop_result.clock_time` gains `effective_utc`, `ct_timezone` and
  `ct_status` (`standard`, `converted`, `ambiguous` or `nonexistent`);
  `recon_started` gains `ct_timezone_rules`. Existing keys keep their meaning:
  `utc` and `local_offset_minutes` stay as broadcast.
- The terminal, browser and desktop dashboards show a converted CT as its
  effective UTC with the source zone, for example `09:06Z ← Europe/Warsaw`,
  log the broadcast value, and mark an ambiguous or nonexistent CT as not
  used. A standard CT looks as before.
- Without any `--ct-timezone` rule, when the consensus differs from the host
  clock by within 2 minutes of a whole number of hours, the dashboard log
  suggests the option once per change of that offset. The host clock is only a
  hint; no time is changed automatically.
- English, Polish and French texts for all of the above, a README section
  "Stations that send local time as UTC" in each language, the operator
  quickstart, and release notes in all three languages.

### Changed

- The desktop window widens its "Last CT" column when a converted or unused CT
  does not fit, and draws the arrow as `<-` because Tk's default fonts may lack
  the glyph.

## [2.0.1] — 2026-11-05

### Fixed

- The consensus no longer reports a time that no station sent. With two equal
  camps, for example two stations an hour ahead of two others, the median fell
  between them; now no source supports the result, so the time is unavailable
  and the stations are listed as outliers.
- Release staging creates the GitHub draft once and waits for the release list
  to show it, even when `gh release create` reports an error such as HTTP 502.
  Several drafts for one tag stop staging with the ids of the empty ones to
  delete; nothing is deleted automatically.
- Finalization waits for PyPI to list freshly uploaded files instead of failing
  at once, while mismatched files still fail immediately. It marks a release
  latest only when no published release has a higher version.

## [2.0.0] — 2026-11-04

### Added

- English, Polish and French interfaces for the terminal, browser and desktop
  dashboards. `recon --lang en|pl|fr` selects the language; English remains
  the default. A shared catalog translates controls, status and operator logs
  while preserving received station data and JSON Lines field names.
- Release notes and a release announcement in all three languages, with
  matching README instructions for WIDE, HOP and language selection. Each
  README shows live screenshots of the three dashboards in its own language.

### Fixed

- Dashboard CT history no longer assigns an observation without confirmed PI
  to a previously identified transmitter.
- The desktop window spells ellipses, typographic apostrophes, dashes and
  quotation marks in ASCII. Tk's bundled fonts have no glyphs for them and drew
  nothing, or unrelated symbols for the ellipsis: the French window showed
  "Suspendre laffichage", and missing values left table cells empty.

- Validate release tags against package, runtime and lockfile versions, test
  the built distributions, and attach assets to a draft before publishing
  the GitHub release. The release becomes public only after PyPI succeeds.
  Reject publication dispatched from a branch or conflicting published assets.
  The release job builds no dependency from source, and its smoke test
  installs the wheel and the locked dependencies by hash.
- Document the release procedure and preserve the published 1.1.0 artifacts.
  Its PyPI upload succeeded; the original GitHub job failed because the release
  had already been created manually.

## [1.1.0] — 2026-11-04

### Added

- `rdsclock recon --jsonl` writes the live loop as JSON Lines on stdout and
  moves the operator display to stderr. Every record carries `event`, `schema`
  and the host clock; `cycle_completed` holds the data of the status block.
  Python callers receive the same typed events through `run_recon(on_event=...)`.
- `rdsclock recon --tui` shows a terminal dashboard (optional extra `tui`, built
  on Textual). It runs `recon --jsonl` as a child process and shows the
  consensus, the watchlist stations and a log; `q` quits, `r` requests a rescan
  and `p` pauses the view. Station names are displayed as plain printable text.
- Live `recon` starts an acquisition scan after the current cycle when it
  receives `SIGUSR1`; `run_recon` accepts a matching `rescan_requested` callback.
- `rdsclock recon --web` serves the same dashboard to a browser using only the
  standard library: Server-Sent Events, a static page and a *Rescan* button.
  It listens on 127.0.0.1 by default and refuses unknown `Host` names. A rescan
  needs a custom header, the same origin and a loopback client, and a strict
  Content-Security-Policy applies.
- `rdsclock recon --gui` shows the same dashboard in a desktop window built with
  tkinter, with Rescan, Pause view and Quit (also `r`, `p`, `q`). Closing the
  window or `Ctrl+C` stops the receiver, and the window forwards `SIGUSR1`. CI
  runs the window tests under a virtual X display.
- `rdsclock recon --hop FREQS` hops between the listed stations instead of
  scanning the band: an acquisition checks only them and keeps every one with
  RDS. `acquisition_started` counts them in `n_points`, and the dashboards show
  the scan as stations rather than tuning points.
- `rdsclock recon --wide FREQS` records stations that fit in the receiver
  bandwidth from one wideband capture per cycle (`--center`, `--fs`) instead of
  scanning the band and hopping. All of them see the same minute edge, and the
  dashboards show the capture centre. `acquisition_started` and
  `maintenance_started` gain `wide_center_hz` and `wide_sample_rate`. The
  default centre keeps every station clear of the DC offset and of the other
  stations' mirror images; halfway between two stations, each would sit on the
  other's image.

### Changed

- The channelizer mixes block by block and decimates with one polyphase filter.
  A 75-second capture at 2.4 MS/s now needs about 3.4 GB of memory instead of
  more than 30 GB, and extraction is several times faster. `decode_channels`
  reports the power of each channel and can anchor Clock-Time receive times to
  the capture start.

### Fixed

- Date each capture by when its samples arrived. While the client decodes,
  rtl_tcp queues samples and then sends them faster than real time, and the
  drain could stop between two bursts of that backlog: the first seconds of a
  capture were older than its start time, and every Clock-Time receive time
  from it was late by as much. `recon --wide`, which decodes for about 45 s
  between captures, was exposed every cycle. The start now moves back to the
  earliest time that every read allows; a sample lost in the middle of a
  capture cannot move it later, as anchoring at the capture end would. This
  also removes up to one rtl_tcp transfer of lateness from other captures
  (up to 0.52 s at 250 kS/s). A capture that ends while its backlog still
  arrives ahead of real time can only bound its start. That holds for any
  capture that began more than one transfer behind and whose last reads did
  not show the stream in real time, even after a stall. `caught_up` marks such
  a capture, and its Clock-Time is reported but kept out of the consensus.
  `FreshCapture` gains `capture_end_monotonic_ns`, `queued_s`, `backlogged` and
  `caught_up`, `capture_fresh` takes the server's `transfer_samples`, and
  `hop_result` gains `queued_s` and `caught_up` (JSON Lines schema 1,
  additive). Verbose output names every backlogged capture.
- Long captures choose a fresh symbol offset every 4096 bits (about 3.4 s).
  A single offset slipped by whole bits when a station's bit clock ran tens of
  ppm away from the receiver's: in one live 75-second capture, Eska ROCK went
  from 450 to 855 decoded groups and Radio Kolor from 591 to 810.
- Actively discard buffered IQ after retuning, with bounded acquisition and
  explicit failure when a capture boundary cannot be established. Live capture
  duration now excludes settling; `--settle-seconds` adds discard time before
  the full recording instead of trimming samples from it.
- Close failed header/capture connections deterministically, including partial
  I/Q reads, and reset digital AGC, direct sampling, test mode and PPM on FM setup.
- Keep PS, RadioText and Clock-Time payloads isolated by PI. Decode totals cover
  all valid groups; station-specific group counts describe the selected PI.
- Anchor consensus to estimated CT reception and deduplicate channels and
  confirmed PI codes before assigning votes. Unconfirmed sources cannot create
  a HIGH-confidence result; stale/future samples are excluded.
- Use a timezone-aware UTC timestamp for settling measurement filenames.
- Close fake receiver sockets and join their threads during test cleanup;
  execute the CLI entrypoint without reusing an imported module. CI now treats
  test warnings as errors.

### Documentation

- The READMEs show how to run each dashboard, with six screenshots of live
  reception in Warsaw: two stations from one WIDE capture (`--wide`, trust
  MEDIUM) and three stations in turn (`--hop`, trust HIGH or MEDIUM).
- Add an original radio-clock SVG banner shared by the English, Polish and
  French READMEs, with localized alternative text.
- Refresh the English and Polish READMEs and add a complete French translation,
  with shared examples and language navigation. Lead with a short synthetic
  demo and measured reception results. Add the November 2 full-band report
  covering 206 tuning points, WIDE captures and three-station CT reception.
- Add a reproducible synthetic/live demonstration and aggregated hardware
  validation for eight frequencies. Document gain tradeoffs and received CT
  disagreement; remove unverified millisecond accuracy claims.
- Clarify that agreement, CRC validity and model uncertainty do not establish
  authenticated or calibrated absolute UTC. Optional local IQ baselines skip
  explicitly when unavailable in a fresh checkout.

### Security

- Replace the archived SonarCloud action with the pinned SonarQube Scan action
  v8.3.0, removing its dependency on the vulnerable v5.0.0 scanner action.
- Freeze CI and release dependencies in `uv.lock`, install third-party wheels
  only, scope publication permissions to their jobs, and pin workflow actions.
- Keep benchmark JSON references and reports within the invocation's working
  directory after canonical path resolution, while preserving external baselines.
- Restrict the settling measurement helper to numeric loopback endpoints and
  standard port 1234, mapping `localhost` without a DNS lookup.

## [1.0.1] — 2026-05-18

### Fixed

- HOP-mode retunes no longer let the previous station's PI contaminate the
  next station. `rdsclock multi --mode hop` now trims the first 2.0 seconds
  from each per-station capture before decoding, skipping RTL-SDR PLL, AGC,
  and Costas-loop settling after every retune. `rdsclock live` applies the
  same default trim for single-station captures.
- `StationInfo.pi` now reports the dominant PI code after at least three
  valid block-A observations instead of the first PI ever seen. The first
  observed PI remains available as `StationInfo.first_pi` for callers that
  need the old diagnostic value.

### Added

- New `--settle-seconds FLOAT` CLI option for `rdsclock live` and HOP-mode
  `rdsclock multi`, defaulting to `2.0`.

## [1.0.0] — 2026-05-18

### Added

- Polish README (`README.pl.md`) translated as a neutral technical document
  with the same structure, diagrams, and command/API examples as the English
  README.
- Bilingual README cross-reference links at the top of both README files.
- `tools/benchmark.py --check FILE.json`, a regression gate that fails on
  lower per-station group counts, changed stable PI codes, or per-station
  decode-time increases above 50%.
- Public top-level imports for the supported API:
  `decode_file`, `decode_iq`, `DecodeResult`, `ClockTime`, `StationInfo`,
  `TimeConsensus`, and `SubSecondEstimate`.

### Changed

- API surface frozen at 1.0.0 (`from rdsclock import ...`). Internal helpers
  under `rdsclock.*` modules may still change; the top-level `__all__` is the
  support contract.
- README quick start now documents the complete passive time-receiver path:
  `rtl_tcp`, band scan, continuous `recon` consensus, optional multi-station
  baseline recording, and the Python sub-second consensus API.
- Package metadata now marks the project as
  `Development Status :: 5 - Production/Stable`.

### Release Line Summary

- 0.1.0–0.1.2 established the synthetic baseline: the package generated and
  decoded its own IQ reliably, but the initial line was broken on real
  broadcasts because both synth and decoder used the same non-RDS coding
  assumption.
- 0.2.0 fixed the real-broadcast decoder by moving to proper biphase
  (Manchester) handling.
- 0.2.1 delivered the decoder hot-path optimisation, about a 7× speedup on
  the baseline IQ corpus while preserving group counts and PI codes.
- 0.2.2 added FM audio extraction and validated Programme Service handling so
  dynamic-PS rotations no longer overwrite the displayed PS with mixed frames.
- 0.3.0 added fault-tolerant block sync with conservative single-bit syndrome
  correction and explicit clean/corrected group counters.
- 0.4.0 added sub-second receive timestamping, pilot-derived bit-rate drift
  correction, and multi-station sub-second consensus.
- 1.0.0 stabilises the public API, adds the Polish README, documents the
  release demo path, and locks benchmark thresholds for future regression
  checks.

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
