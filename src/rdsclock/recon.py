"""Continuous passive RDS time receiver.

Goal: in a GPS-denied environment, maintain an accurate UTC estimate by
listening to multiple FM stations and combining their Clock-Time
broadcasts into a robust consensus that resists a single misbehaving
station. The operator sees: current time + uncertainty (±X s) + the
number of active sources.

Operating modes:
    LIVE     — :func:`run_recon`: real RTL-SDR over ``rtl_tcp``.
    OFFLINE  — :func:`run_recon_offline`: replay over pre-recorded IQ
                files (for demos and integration tests).

Architecture:
    Acquisition  — a quick band scan locates stations with strong RSSI.
                    Stations whose decode yields a Clock-Time enter the
                    watchlist.
    Maintenance  — hop through the watchlist, collect Clock-Time per
                    station, update the consensus, render status.
    Periodic rescan — every ``rescan_min`` minutes a fuller scan is
                    issued to pick up new stations or detect quality drops.

Everything is fully passive: receive-only, no RF emission of any kind.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from .decoder import decode_file, decode_iq
from .rtl_tcp import RtlTcpClient
from .time_consensus import (
    StationFingerprint,
    StationObservation,
    TimeConsensus,
)


@dataclass
class StationCandidate:
    """A station discovered during a band scan."""

    freq_hz: float
    rssi_db: float
    n_groups: int
    has_ct: bool
    pi: int | None
    ps_name: str = ""


@dataclass
class ReconConfig:
    band_start_mhz: float = 87.5
    band_end_mhz: float = 108.0
    scan_step_mhz: float = 0.2  # band scan step
    scan_dwell_s: float = 1.5  # dwell time per scan sample
    rssi_threshold_db: float = -20.0  # minimum power to consider a station
    max_watchlist: int = 5  # cap on watchlist size
    min_for_consensus: int = 2  # min stations required for consensus
    dwell_s: float = 8.0  # dwell time per station in hop loop
    idle_s: float = 1.0  # sleep between main-loop iterations
    rescan_min: float = 10.0  # full rescan period in minutes
    mission_precision_s: float = 60.0  # acceptable uncertainty for the mission
    gain_db: float | None = None  # use AGC when left unset
    sample_rate: int = 250_000
    fs_scan: int = 250_000
    host: str = "localhost"
    port: int = 1234
    iterations: int | None = None  # None = run forever (until Ctrl+C)


def quick_scan_band(
    client: RtlTcpClient,
    cfg: ReconConfig,
    progress: Callable[[str], None] | None = None,
) -> list[StationCandidate]:
    """Quickly scan the FM band. Per frequency: capture ``scan_dwell_s``,
    measure RSSI, and attempt a decode to check for RDS / Clock-Time."""

    def emit(msg: str) -> None:
        if progress:
            progress(msg)

    client.set_sample_rate(cfg.fs_scan)
    if cfg.gain_db is None:
        client.set_gain_mode_auto()
    else:
        client.set_gain_mode_manual(int(cfg.gain_db * 10))

    candidates: list[StationCandidate] = []
    freq = cfg.band_start_mhz
    while freq <= cfg.band_end_mhz + 1e-6:
        client.set_frequency(int(freq * 1e6))
        capture_start_ns = time.monotonic_ns()
        iq = client.read_iq(int(cfg.fs_scan * cfg.scan_dwell_s), settle_s=0.05)
        rssi = 10 * np.log10(float(np.mean(np.abs(iq) ** 2)) + 1e-12)
        if rssi > cfg.rssi_threshold_db:
            result = decode_iq(
                iq,
                fs=cfg.fs_scan,
                capture_start_monotonic_ns=capture_start_ns,
            )
            has_ct = result.info.latest_clock is not None
            candidates.append(
                StationCandidate(
                    freq_hz=freq * 1e6,
                    rssi_db=float(rssi),
                    n_groups=result.n_groups,
                    has_ct=has_ct,
                    pi=result.info.pi,
                    ps_name=result.info.ps_name,
                )
            )
            emit(
                f"  scan {freq:6.2f} MHz: RSSI={rssi:+.1f}dB "
                f"groups={result.n_groups} CT={'YES' if has_ct else '—'}"
            )
        freq += cfg.scan_step_mhz
    return candidates


def rank_candidates(candidates: list[StationCandidate]) -> list[StationCandidate]:
    """Rank candidates by quality: CT presence > group count > RSSI."""
    return sorted(
        candidates,
        key=lambda c: (1 if c.has_ct else 0, c.n_groups, c.rssi_db),
        reverse=True,
    )


def hop_collect_ct(
    client: RtlTcpClient,
    watchlist: list[StationCandidate],
    cfg: ReconConfig,
    consensus: TimeConsensus,
    progress: Callable[[str], None] | None = None,
) -> int:
    """Hop through the watchlist: tune, record, decode, store CT in the consensus.

    Returns the number of new CT observations recorded in this iteration.
    """

    def emit(msg: str) -> None:
        if progress:
            progress(msg)

    client.set_sample_rate(cfg.sample_rate)
    if cfg.gain_db is None:
        client.set_gain_mode_auto()
    else:
        client.set_gain_mode_manual(int(cfg.gain_db * 10))

    n_obs = 0
    for station in watchlist:
        client.set_frequency(int(station.freq_hz))
        capture_start_wall = datetime.now(UTC)
        capture_start_ns = time.monotonic_ns()
        iq = client.read_iq(int(cfg.sample_rate * cfg.dwell_s), settle_s=0.1)
        result = decode_iq(
            iq,
            fs=cfg.sample_rate,
            capture_start_monotonic_ns=capture_start_ns,
        )
        ct = result.info.latest_clock
        if ct is not None:
            received_wall = None
            if ct.rx_monotonic_ns is not None:
                received_wall = capture_start_wall + timedelta(
                    seconds=(ct.rx_monotonic_ns - capture_start_ns) / 1_000_000_000
                )
            fingerprint = StationFingerprint(
                pi_code=result.info.pi,
                cfo_hz=result.freq_offset_hz,
                rssi_db=10 * np.log10(float(np.mean(np.abs(iq) ** 2)) + 1e-12),
            )
            obs = StationObservation(
                freq_hz=station.freq_hz,
                pi=result.info.pi,
                ct_utc=ct.utc,
                received_monotonic=time.monotonic(),
                fingerprint=fingerprint,
                clock_time=ct,
                rx_monotonic_ns=ct.rx_monotonic_ns,
                received_wall_utc=received_wall,
            )
            consensus.record(obs)
            n_obs += 1
            emit(
                f"  hop {station.freq_hz / 1e6:6.2f} MHz: "
                f"CT={ct.utc.strftime('%H:%M:%S')}Z groups={result.n_groups}"
            )
        else:
            emit(f"  hop {station.freq_hz / 1e6:6.2f} MHz: no CT (groups={result.n_groups})")
    return n_obs


def render_status(
    consensus: TimeConsensus,
    watchlist: list[StationCandidate],
    next_rescan_in_s: float,
    system_now: datetime | None = None,
) -> str:
    """Render a TTY-style status block for the operator."""
    if system_now is None:
        system_now = datetime.now(UTC)
    result = consensus.consensus()
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("  rdsclock recon — passive RDS time receiver")
    lines.append("=" * 78)
    lines.append(f"  CONSENSUS: {result.format_display()}")
    if result.utc is not None:
        delta_s = (system_now - result.utc).total_seconds()
        lines.append(f"  SYSTEM:    {system_now.strftime('%Y-%m-%d %H:%M:%S')}Z  Δ={delta_s:+.1f}s")
    else:
        lines.append(f"  SYSTEM:    {system_now.strftime('%Y-%m-%d %H:%M:%S')}Z  (no consensus)")
    if result.outlier_freqs_mhz:
        lines.append(
            "  OUTLIERS:  " + ", ".join(f"{f:.2f}" for f in result.outlier_freqs_mhz) + " MHz"
        )
    lines.append("")
    lines.append(f"  Watchlist ({len(watchlist)}):")
    if watchlist:
        lines.append(f"    {'freq':<10} {'PS':<10} {'PI':<8} {'RSSI':<7} {'grp':<5} {'CT?'}")
        for s in watchlist:
            pi = f"0x{s.pi:04X}" if s.pi is not None else "—"
            lines.append(
                f"    {s.freq_hz / 1e6:<10.2f} {s.ps_name:<10} {pi:<8} "
                f"{s.rssi_db:<+7.1f} {s.n_groups:<5} "
                f"{'YES' if s.has_ct else 'no'}"
            )
    else:
        lines.append("    (empty — acquisition in progress)")
    lines.append("")
    lines.append(consensus.summary())
    lines.append("")
    lines.append(f"  Next rescan in: {next_rescan_in_s:.0f}s")
    lines.append("=" * 78)
    return "\n".join(lines)


def run_recon(
    cfg: ReconConfig,
    on_status: Callable[[str], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> None:
    """Run the live recon loop. Blocking; interrupt with Ctrl+C."""
    consensus = TimeConsensus(mission_precision_s=cfg.mission_precision_s)
    watchlist: list[StationCandidate] = []
    last_rescan_mono = float("-inf")
    iteration = 0

    if on_status is None:
        on_status = print

    with RtlTcpClient(host=cfg.host, port=cfg.port) as client:
        info = client.info
        on_status(f"  rtl_tcp OK: tuner={info.tuner_type}, gains={info.gain_count}")
        try:
            while True:
                iteration += 1
                now_mono = time.monotonic()
                # ACQUISITION when watchlist is empty or rescan_min has elapsed.
                if not watchlist or (now_mono - last_rescan_mono) > cfg.rescan_min * 60:
                    on_status(
                        f"\n[#{iteration}] ACQUISITION "
                        f"(scan {cfg.band_start_mhz}-{cfg.band_end_mhz} MHz)…"
                    )
                    candidates = quick_scan_band(client, cfg, progress=on_progress)
                    ranked = rank_candidates(candidates)
                    watchlist = [c for c in ranked if c.has_ct][: cfg.max_watchlist]
                    if not watchlist:
                        # Fallback: when no station yields CT, keep stations with RDS.
                        watchlist = [c for c in ranked if c.n_groups > 0][: cfg.max_watchlist]
                    last_rescan_mono = time.monotonic()
                    on_status(f"  → watchlist: {[f'{s.freq_hz / 1e6:.2f}' for s in watchlist]}")

                # MAINTENANCE — hop
                on_status(f"\n[#{iteration}] MAINTENANCE (hop {len(watchlist)} stations)…")
                new_obs = hop_collect_ct(client, watchlist, cfg, consensus, progress=on_progress)
                on_status(f"  collected {new_obs} new CT observations")

                # Display
                next_rescan_in = max(
                    0.0, cfg.rescan_min * 60 - (time.monotonic() - last_rescan_mono)
                )
                on_status("\n" + render_status(consensus, watchlist, next_rescan_in))

                if cfg.iterations is not None and iteration >= cfg.iterations:
                    on_status(f"\n[#{iteration}] reached iterations={cfg.iterations}, stop")
                    break
                if cfg.idle_s > 0:
                    time.sleep(cfg.idle_s)
        except KeyboardInterrupt:
            on_status("\nInterrupted by operator (Ctrl+C).")


# ----------------------- OFFLINE / DEMO MODE -----------------------

_FREQ_RE = re.compile(r"(\d{2,3}\.\d{1,2})")


def _parse_freq_from_filename(name: str) -> float | None:
    """Extract the MHz value from a filename like ``fm_098.30_MHz.iq``
    or ``live_98.3.iq``."""
    m = _FREQ_RE.search(name)
    return float(m.group(1)) if m else None


def run_recon_offline(
    cfg: ReconConfig,
    recordings_dir: str,
    on_status: Callable[[str], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
    limit_files: int | None = None,
) -> TimeConsensus:
    """Replay recon over a directory of pre-recorded IQ files.

    Each ``*.iq`` file is treated as one "hop": decode it, push any
    Clock-Time into the consensus, and emit a status line. Returns the
    populated :class:`TimeConsensus` for downstream analysis or tests.

    ``on_progress`` is forwarded to the decoder so that callers can
    surface per-stage diagnostics when running in verbose mode.
    """
    if on_status is None:
        on_status = print

    consensus = TimeConsensus(mission_precision_s=cfg.mission_precision_s)
    rec_dir = Path(recordings_dir)
    files = sorted(rec_dir.glob("*.iq"))
    if limit_files is not None:
        files = files[:limit_files]
    on_status(f"  offline: {len(files)} files in {rec_dir}")

    watchlist_freqs: list[float] = []
    # In offline mode all files are treated as "now" snapshots — we use a
    # single monotonic timestamp for every observation so the consensus
    # compares raw CT values without artificial ageing.
    mono_t = time.monotonic()
    snapshot_mono = mono_t
    for i, path in enumerate(files, 1):
        freq_mhz = _parse_freq_from_filename(path.name)
        if freq_mhz is None:
            on_status(f"  ⚠ skipping {path.name} (cannot parse MHz)")
            continue
        try:
            result = decode_file(str(path), fs=cfg.sample_rate, progress=on_progress)
        except Exception as exc:
            on_status(f"  ⚠ {path.name}: {type(exc).__name__}: {exc}")
            continue

        ct = result.info.latest_clock
        if ct is not None:
            obs = StationObservation(
                freq_hz=freq_mhz * 1e6,
                pi=result.info.pi,
                ct_utc=ct.utc,
                received_monotonic=snapshot_mono,
                fingerprint=StationFingerprint(
                    pi_code=result.info.pi,
                    cfo_hz=result.freq_offset_hz,
                ),
            )
            consensus.record(obs)
            if freq_mhz not in watchlist_freqs:
                watchlist_freqs.append(freq_mhz)
            on_status(
                f"  [{i}/{len(files)}] {freq_mhz:.2f} MHz: "
                f"PS='{result.info.ps_name}' CT={ct.utc.strftime('%H:%M:%S')}Z "
                f"groups={result.n_groups}"
            )
        else:
            on_status(
                f"  [{i}/{len(files)}] {freq_mhz:.2f} MHz: no CT "
                f"(groups={result.n_groups}, PS='{result.info.ps_name}')"
            )

        # Each file simulates ~60s of wall-clock time (typical recording length).
        mono_t += 60.0

    # Render the final consensus.
    result = consensus.consensus(monotonic_now=snapshot_mono)
    on_status("")
    on_status("=" * 78)
    on_status("  OFFLINE RECON — final consensus")
    on_status("=" * 78)
    on_status(f"  {result.format_display()}")
    if result.outlier_freqs_mhz:
        on_status(f"  OUTLIERS: {result.outlier_freqs_mhz}")
    on_status("")
    on_status(consensus.summary(monotonic_now=mono_t))
    on_status("=" * 78)
    return consensus
