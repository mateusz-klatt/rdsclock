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
                    watchlist. With ``hop_freqs_hz`` it checks only the
                    listed stations and keeps every one with RDS.
    Maintenance  — hop through the watchlist, collect Clock-Time per
                    station, update the consensus, render status.
    WIDE         — with ``wide_freqs_hz``, one wideband capture records
                    all these stations at once and replaces both the band
                    scan and the hops; the channelizer separates them.
    Periodic rescan — every ``rescan_min`` minutes a fuller scan is
                    issued to pick up new stations or detect quality drops.

Events:
    Every operator message of the live loop has a typed counterpart
    (:data:`ReconEvent`) delivered through ``on_event``.
    :func:`event_to_json` renders one as a JSON Lines record; this is the
    output of ``rdsclock recon --jsonl``.

Everything is fully passive: receive-only, no RF emission of any kind.
"""

import json
import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import ClassVar

import numpy as np

from .channelizer import ChannelDecodeResult, ChannelSpec, choose_center, decode_channels
from .decoder import DecodeResult, decode_file, decode_iq
from .rds_clock import ClockTime
from .rtl_tcp import RtlTcpClient
from .time_consensus import (
    ConsensusResult,
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


WIDE_SAMPLE_RATE = 2_400_000  # default WIDE capture rate, as for ``rdsclock multi``


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
    settle_s: float = 0.3  # actively discard queued IQ after retuning
    # HOP over a list: when set, acquisitions check only these stations
    # instead of scanning the band.
    hop_freqs_hz: tuple[float, ...] = ()
    # WIDE: when set, one capture records all these stations at once,
    # replacing the band scan and the hops.
    wide_freqs_hz: tuple[float, ...] = ()
    wide_center_hz: float | None = None  # None: chosen by wide_center()
    wide_sample_rate: int = WIDE_SAMPLE_RATE

    def wide_center(self) -> float:
        """The WIDE capture centre: ``wide_center_hz``, or one away from the stations."""
        if self.wide_center_hz is not None:
            return self.wide_center_hz
        return choose_center(self.wide_freqs_hz, self.wide_sample_rate)


# ----------------------- EVENTS -----------------------
#
# Typed counterparts of the operator messages, for programs that follow the
# loop without parsing console text. Bump the schema version only for
# changes that break existing readers; new events and fields do not.

JSONL_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TrackSnapshot:
    """One row of :meth:`TimeConsensus.summary` as data.

    ``freq_hz`` is the tuning of the track's latest observation.
    """

    freq_hz: float
    pi: int | None
    n_observations: int
    trust_score: float
    consecutive_outliers: int
    age_s: float
    estimated_utc: datetime | None


@dataclass(frozen=True)
class ReconStarted:
    """``rtl_tcp`` answered; the loop is about to start."""

    kind: ClassVar[str] = "recon_started"
    tuner_type: int
    gain_count: int


@dataclass(frozen=True)
class AcquisitionStarted:
    """A band scan is starting; it chooses the next watchlist.

    In WIDE mode one capture at ``wide_center_hz`` replaces the scan: the band
    spans the listed stations and ``scan_step_mhz`` is 0. A scan of listed
    HOP stations also reports a step of 0, and ``n_points`` counts them.
    """

    kind: ClassVar[str] = "acquisition_started"
    iteration: int
    band_start_mhz: float
    band_end_mhz: float
    scan_step_mhz: float
    wide_center_hz: float | None = None
    wide_sample_rate: int | None = None
    n_points: int | None = None


@dataclass(frozen=True)
class ScanStep:
    """One scanned frequency; ``candidate`` is ``None`` at or below the RSSI threshold."""

    kind: ClassVar[str] = "scan_step"
    freq_hz: float
    rssi_db: float
    candidate: StationCandidate | None


@dataclass(frozen=True)
class WatchlistUpdated:
    """Acquisition chose the stations that the following hops visit."""

    kind: ClassVar[str] = "watchlist_updated"
    iteration: int
    watchlist: tuple[StationCandidate, ...]


@dataclass(frozen=True)
class MaintenanceStarted:
    """The hop loop is about to visit every watchlist station once.

    In WIDE mode one capture at ``wide_center_hz`` records them all instead.
    """

    kind: ClassVar[str] = "maintenance_started"
    iteration: int
    n_stations: int
    wide_center_hz: float | None = None
    wide_sample_rate: int | None = None


@dataclass(frozen=True)
class HopResult:
    """One watchlist station recorded and decoded; ``clock_time`` is ``None`` without CT."""

    kind: ClassVar[str] = "hop_result"
    freq_hz: float
    pi: int | None
    ps_name: str
    rssi_db: float
    n_groups: int
    n_groups_clean: int
    n_groups_corrected: int
    clock_time: ClockTime | None


@dataclass(frozen=True)
class CycleCompleted:
    """The data behind the status block rendered after every iteration."""

    kind: ClassVar[str] = "cycle_completed"
    iteration: int
    new_observations: int
    consensus: ConsensusResult
    watchlist: tuple[StationCandidate, ...]
    tracks: tuple[TrackSnapshot, ...]
    next_rescan_in_s: float


@dataclass(frozen=True)
class ReconStopped:
    """The loop ended without an error: ``reason`` is ``"iterations"`` or ``"interrupted"``."""

    kind: ClassVar[str] = "recon_stopped"
    iteration: int
    reason: str


ReconEvent = (
    ReconStarted
    | AcquisitionStarted
    | ScanStep
    | WatchlistUpdated
    | MaintenanceStarted
    | HopResult
    | CycleCompleted
    | ReconStopped
)


def event_to_json(event: ReconEvent, host_utc: datetime | None = None) -> str:
    """Render ``event`` as one JSON Lines record, without the trailing newline.

    The record starts with ``event`` (the kind), ``schema`` and ``host_utc``:
    the host clock when the record was rendered, not a verified time source.
    The remaining keys mirror the dataclass fields. Datetimes become ISO 8601
    strings, enums their values, and infinite floats ``null``.
    """
    record = {
        "event": event.kind,
        "schema": JSONL_SCHEMA_VERSION,
        "host_utc": host_utc if host_utc is not None else datetime.now(UTC),
        **asdict(event),
    }
    return json.dumps(_jsonable(record), allow_nan=False, separators=(",", ":"))


def _jsonable(value: object) -> object:
    """Convert the values that :func:`json.dumps` cannot encode as JSON."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _scan_tunings_hz(cfg: ReconConfig) -> list[float]:
    """The tunings of an acquisition scan: the listed HOP stations, or the band."""
    if cfg.hop_freqs_hz:
        return list(cfg.hop_freqs_hz)
    tunings = []
    freq = cfg.band_start_mhz
    while freq <= cfg.band_end_mhz + 1e-6:
        tunings.append(freq * 1e6)
        freq += cfg.scan_step_mhz
    return tunings


def quick_scan_band(
    client: RtlTcpClient,
    cfg: ReconConfig,
    progress: Callable[[str], None] | None = None,
    on_event: Callable[[ReconEvent], None] | None = None,
) -> list[StationCandidate]:
    """Quickly scan the FM band. Per frequency: capture ``scan_dwell_s``,
    measure RSSI, and attempt a decode to check for RDS / Clock-Time.

    With ``cfg.hop_freqs_hz`` only the listed stations are checked, and the
    RSSI threshold does not apply to them. ``on_event`` receives a
    :class:`ScanStep` for every frequency, including those that were not
    decoded because of the RSSI threshold."""

    def emit(msg: str) -> None:
        if progress:
            progress(msg)

    client.configure_fm(cfg.fs_scan, gain_db=cfg.gain_db)

    candidates: list[StationCandidate] = []
    for freq_hz in _scan_tunings_hz(cfg):
        client.set_frequency(int(freq_hz))
        capture = client.capture_fresh(cfg.scan_dwell_s, cfg.fs_scan, settle_s=cfg.settle_s)
        iq = capture.iq
        rssi = 10 * np.log10(float(np.mean(np.abs(iq) ** 2)) + 1e-12)
        candidate = None
        if cfg.hop_freqs_hz or rssi > cfg.rssi_threshold_db:
            result = decode_iq(
                iq,
                fs=cfg.fs_scan,
                capture_start_monotonic_ns=capture.capture_start_monotonic_ns,
            )
            has_ct = result.info.latest_clock is not None
            candidate = StationCandidate(
                freq_hz=freq_hz,
                rssi_db=float(rssi),
                n_groups=result.n_groups,
                has_ct=has_ct,
                pi=result.info.pi,
                ps_name=result.info.ps_name,
            )
            candidates.append(candidate)
            emit(
                f"  scan {freq_hz / 1e6:6.2f} MHz: RSSI={rssi:+.1f}dB "
                f"groups={result.n_groups} CT={'YES' if has_ct else '—'}"
            )
        if on_event:
            snapshot = None if candidate is None else replace(candidate)
            on_event(ScanStep(freq_hz=freq_hz, rssi_db=float(rssi), candidate=snapshot))
    return candidates


def rank_candidates(candidates: list[StationCandidate]) -> list[StationCandidate]:
    """Rank candidates by quality: CT presence > group count > RSSI."""
    return sorted(
        candidates,
        key=lambda c: (1 if c.has_ct else 0, c.n_groups, c.rssi_db),
        reverse=True,
    )


def _capture_start_wall(capture_start_ns: int) -> datetime:
    """Map a capture's host monotonic start onto wall time.

    Call it right after the capture, so that neither the duration of the
    capture nor the decoding that follows enters the CT anchor.
    """
    wall_anchor = datetime.now(UTC)
    monotonic_anchor = time.monotonic_ns()
    return wall_anchor - timedelta(seconds=(monotonic_anchor - capture_start_ns) / 1e9)


def _record_station(
    freq_hz: float,
    result: DecodeResult,
    rssi_db: float,
    capture_start: tuple[int, datetime],
    consensus: TimeConsensus,
) -> HopResult:
    """Store a decoded station's CT in the consensus and describe the station.

    ``capture_start`` is the capture's monotonic and wall-time start.
    """
    capture_start_ns, capture_start_wall = capture_start
    ct = result.info.latest_clock
    if ct is not None:
        received_wall = None
        if ct.rx_monotonic_ns is not None:
            received_wall = capture_start_wall + timedelta(
                seconds=(ct.rx_monotonic_ns - capture_start_ns) / 1_000_000_000
            )
        fingerprint = StationFingerprint(
            pi_code=result.info.pi,
            cfo_hz=None,  # the decoder currently does not measure RF CFO
            rssi_db=rssi_db,
        )
        obs = StationObservation(
            freq_hz=freq_hz,
            pi=result.info.pi,
            ct_utc=ct.utc,
            received_monotonic=(
                ct.rx_monotonic_ns / 1e9
                if ct.rx_monotonic_ns is not None
                else capture_start_ns / 1e9
            ),
            fingerprint=fingerprint,
            clock_time=ct,
            rx_monotonic_ns=ct.rx_monotonic_ns,
            received_wall_utc=received_wall,
        )
        consensus.record(obs)
    return HopResult(
        freq_hz=freq_hz,
        pi=result.info.pi,
        ps_name=result.info.ps_name,
        rssi_db=rssi_db,
        n_groups=result.n_groups,
        n_groups_clean=result.n_groups_clean,
        n_groups_corrected=result.n_groups_corrected,
        clock_time=ct,
    )


def _report_station(
    mode: str,
    hop: HopResult,
    progress: Callable[[str], None] | None,
    on_event: Callable[[ReconEvent], None] | None,
) -> int:
    """Pass one station's result on; return 1 when it carried CT, otherwise 0."""
    if progress:
        if hop.clock_time is not None:
            progress(
                f"  {mode} {hop.freq_hz / 1e6:6.2f} MHz: "
                f"CT={hop.clock_time.utc.strftime('%H:%M:%S')}Z groups={hop.n_groups}"
            )
        else:
            progress(f"  {mode} {hop.freq_hz / 1e6:6.2f} MHz: no CT (groups={hop.n_groups})")
    if on_event:
        on_event(hop)
    return 0 if hop.clock_time is None else 1


def hop_collect_ct(
    client: RtlTcpClient,
    watchlist: list[StationCandidate],
    cfg: ReconConfig,
    consensus: TimeConsensus,
    progress: Callable[[str], None] | None = None,
    on_event: Callable[[ReconEvent], None] | None = None,
) -> int:
    """Hop through the watchlist: tune, record, decode, store CT in the consensus.

    Returns the number of new CT observations recorded in this iteration.
    ``on_event`` receives a :class:`HopResult` for every station, with or
    without CT.
    """
    client.configure_fm(cfg.sample_rate, gain_db=cfg.gain_db)

    n_obs = 0
    for station in watchlist:
        client.set_frequency(int(station.freq_hz))
        capture = client.capture_fresh(cfg.dwell_s, cfg.sample_rate, settle_s=cfg.settle_s)
        iq = capture.iq
        capture_start_ns = capture.capture_start_monotonic_ns
        capture_start = (capture_start_ns, _capture_start_wall(capture_start_ns))
        result = decode_iq(
            iq,
            fs=cfg.sample_rate,
            capture_start_monotonic_ns=capture_start_ns,
        )
        rssi_db = float(10 * np.log10(float(np.mean(np.abs(iq) ** 2)) + 1e-12))
        hop = _record_station(station.freq_hz, result, rssi_db, capture_start, consensus)
        n_obs += _report_station("hop", hop, progress, on_event)
    return n_obs


def _wide_capture(
    client: RtlTcpClient, cfg: ReconConfig, freqs_hz: Sequence[float], duration_s: float
) -> tuple[list[ChannelDecodeResult], tuple[int, datetime]]:
    """Record ``duration_s`` around the WIDE centre and decode ``freqs_hz`` from it.

    Returns the results per station and the capture's monotonic and
    wall-time start.
    """
    center_hz = cfg.wide_center()
    client.configure_fm(cfg.wide_sample_rate, gain_db=cfg.gain_db)
    client.set_frequency(int(center_hz))
    capture = client.capture_fresh(duration_s, cfg.wide_sample_rate, settle_s=cfg.settle_s)
    capture_start_ns = capture.capture_start_monotonic_ns
    capture_start = (capture_start_ns, _capture_start_wall(capture_start_ns))
    # One station at a time: each extraction holds a copy of the capture.
    results = decode_channels(
        iq_wide=capture.iq,
        fs_wide=cfg.wide_sample_rate,
        f_center=center_hz,
        channels=[ChannelSpec(freq_hz=freq_hz) for freq_hz in freqs_hz],
        max_workers=1,
        capture_start_monotonic_ns=capture_start_ns,
    )
    return results, capture_start


def wide_scan(
    client: RtlTcpClient,
    cfg: ReconConfig,
    progress: Callable[[str], None] | None = None,
    on_event: Callable[[ReconEvent], None] | None = None,
) -> list[StationCandidate]:
    """WIDE acquisition: decode every station of ``cfg.wide_freqs_hz`` from one
    ``scan_dwell_s`` capture instead of scanning the band.

    ``on_event`` receives a :class:`ScanStep` for every station. The RSSI
    threshold does not apply: the capture contains every station anyway.
    """
    results, _ = _wide_capture(client, cfg, cfg.wide_freqs_hz, cfg.scan_dwell_s)
    candidates: list[StationCandidate] = []
    for channel in results:
        info = channel.result.info
        candidate = StationCandidate(
            freq_hz=channel.spec.freq_hz,
            rssi_db=channel.rssi_db,
            n_groups=channel.result.n_groups,
            has_ct=info.latest_clock is not None,
            pi=info.pi,
            ps_name=info.ps_name,
        )
        candidates.append(candidate)
        if progress:
            progress(
                f"  wide {candidate.freq_hz / 1e6:6.2f} MHz: RSSI={candidate.rssi_db:+.1f}dB "
                f"groups={candidate.n_groups} CT={'YES' if candidate.has_ct else '—'}"
            )
        if on_event:
            on_event(
                ScanStep(
                    freq_hz=candidate.freq_hz,
                    rssi_db=candidate.rssi_db,
                    candidate=replace(candidate),
                )
            )
    return candidates


def wide_collect_ct(
    client: RtlTcpClient,
    watchlist: list[StationCandidate],
    cfg: ReconConfig,
    consensus: TimeConsensus,
    progress: Callable[[str], None] | None = None,
    on_event: Callable[[ReconEvent], None] | None = None,
) -> int:
    """WIDE maintenance: record every watchlist station from one ``dwell_s``
    capture and store their CT in the consensus.

    Returns the number of new CT observations. ``on_event`` receives a
    :class:`HopResult` for every station, as after a hop. An empty watchlist
    records nothing.
    """
    if not watchlist:
        return 0
    freqs_hz = [station.freq_hz for station in watchlist]
    results, capture_start = _wide_capture(client, cfg, freqs_hz, cfg.dwell_s)
    n_obs = 0
    for channel in results:
        hop = _record_station(
            channel.spec.freq_hz, channel.result, channel.rssi_db, capture_start, consensus
        )
        n_obs += _report_station("wide", hop, progress, on_event)
    return n_obs


def render_status(
    consensus: TimeConsensus,
    watchlist: list[StationCandidate],
    next_rescan_in_s: float,
    system_now: datetime | None = None,
    result: ConsensusResult | None = None,
) -> str:
    """Render a TTY-style status block for the operator.

    Pass ``result`` when the caller has already evaluated the consensus:
    every evaluation updates the per-station trust scores.
    """
    if system_now is None:
        system_now = datetime.now(UTC)
    if result is None:
        result = consensus.consensus()
    lines: list[str] = [
        "=" * 78,
        "  rdsclock recon — passive RDS time receiver",
        "=" * 78,
        f"  CONSENSUS: {result.format_display()}",
        "  ± describes source dispersion/holdover; absolute UTC accuracy is uncalibrated.",
    ]
    if result.utc is not None:
        delta_s = (system_now - result.utc).total_seconds()
        lines.append(f"  SYSTEM:    {system_now.strftime('%Y-%m-%d %H:%M:%S')}Z  Δ={delta_s:+.1f}s")
    else:
        lines.append(f"  SYSTEM:    {system_now.strftime('%Y-%m-%d %H:%M:%S')}Z  (no consensus)")
    if result.outlier_freqs_mhz:
        lines.append(
            "  OUTLIERS:  " + ", ".join(f"{f:.2f}" for f in result.outlier_freqs_mhz) + " MHz"
        )
    lines.extend(["", f"  Watchlist ({len(watchlist)}):"])
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
    lines.extend(
        [
            "",
            consensus.summary(),
            "",
            f"  Next rescan in: {next_rescan_in_s:.0f}s",
            "=" * 78,
        ]
    )
    return "\n".join(lines)


def _detached(stations: list[StationCandidate]) -> tuple[StationCandidate, ...]:
    """Copies for an event, so that a consumer cannot change the live watchlist."""
    return tuple(replace(station) for station in stations)


def _track_snapshots(consensus: TimeConsensus, monotonic_now: float) -> tuple[TrackSnapshot, ...]:
    """The rows of :meth:`TimeConsensus.summary`, in the same order."""
    return tuple(
        TrackSnapshot(
            freq_hz=t.latest_freq_hz,
            pi=t.pi,
            n_observations=len(t.observations),
            trust_score=t.trust_score,
            consecutive_outliers=t.consecutive_outliers,
            age_s=t.age_s(monotonic_now),
            estimated_utc=t.estimated_utc_now(monotonic_now),
        )
        for t in sorted(consensus.tracks.values(), key=lambda x: -x.trust_score)
    )


@dataclass(frozen=True)
class _Outputs:
    """Where the live loop reports: operator text, optional detail and events."""

    status: Callable[[str], None]
    progress: Callable[[str], None] | None = None
    event: Callable[[ReconEvent], None] | None = None

    def publish(self, event: ReconEvent) -> None:
        if self.event:
            self.event(event)


def _acquire(
    client: RtlTcpClient, cfg: ReconConfig, iteration: int, out: _Outputs
) -> list[StationCandidate]:
    """Run one acquisition and return the next watchlist."""
    if cfg.wide_freqs_hz:
        center_hz = cfg.wide_center()
        out.status(f"\n[#{iteration}] ACQUISITION (WIDE capture at {center_hz / 1e6:.3f} MHz)…")
        out.publish(
            AcquisitionStarted(
                iteration=iteration,
                band_start_mhz=min(cfg.wide_freqs_hz) / 1e6,
                band_end_mhz=max(cfg.wide_freqs_hz) / 1e6,
                scan_step_mhz=0.0,
                wide_center_hz=center_hz,
                wide_sample_rate=cfg.wide_sample_rate,
            )
        )
        ranked = rank_candidates(wide_scan(client, cfg, out.progress, out.event))
        # One capture records them all, so every station with RDS stays.
        return [c for c in ranked if c.n_groups > 0]

    if cfg.hop_freqs_hz:
        out.status(f"\n[#{iteration}] ACQUISITION (check {len(cfg.hop_freqs_hz)} listed stations)…")
        out.publish(
            AcquisitionStarted(
                iteration=iteration,
                band_start_mhz=min(cfg.hop_freqs_hz) / 1e6,
                band_end_mhz=max(cfg.hop_freqs_hz) / 1e6,
                scan_step_mhz=0.0,
                n_points=len(cfg.hop_freqs_hz),
            )
        )
        ranked = rank_candidates(quick_scan_band(client, cfg, out.progress, out.event))
        # The operator chose these stations: keep every one with RDS, uncapped.
        return [c for c in ranked if c.n_groups > 0]

    out.status(f"\n[#{iteration}] ACQUISITION (scan {cfg.band_start_mhz}-{cfg.band_end_mhz} MHz)…")
    out.publish(
        AcquisitionStarted(
            iteration=iteration,
            band_start_mhz=cfg.band_start_mhz,
            band_end_mhz=cfg.band_end_mhz,
            scan_step_mhz=cfg.scan_step_mhz,
        )
    )
    ranked = rank_candidates(
        quick_scan_band(client, cfg, progress=out.progress, on_event=out.event)
    )
    watchlist = [c for c in ranked if c.has_ct][: cfg.max_watchlist]
    if not watchlist:
        # Fallback: when no station yields CT, keep stations with RDS.
        watchlist = [c for c in ranked if c.n_groups > 0][: cfg.max_watchlist]
    return watchlist


def _maintain(
    client: RtlTcpClient,
    cfg: ReconConfig,
    iteration: int,
    watchlist: list[StationCandidate],
    consensus: TimeConsensus,
    out: _Outputs,
) -> int:
    """Observe the watchlist once; return the number of new CT observations."""
    if cfg.wide_freqs_hz:
        center_hz = cfg.wide_center()
        out.status(
            f"\n[#{iteration}] MAINTENANCE (WIDE capture of {len(watchlist)} stations "
            f"at {center_hz / 1e6:.3f} MHz)…"
        )
        out.publish(
            MaintenanceStarted(
                iteration=iteration,
                n_stations=len(watchlist),
                wide_center_hz=center_hz,
                wide_sample_rate=cfg.wide_sample_rate,
            )
        )
        return wide_collect_ct(client, watchlist, cfg, consensus, out.progress, out.event)

    out.status(f"\n[#{iteration}] MAINTENANCE (hop {len(watchlist)} stations)…")
    out.publish(MaintenanceStarted(iteration=iteration, n_stations=len(watchlist)))
    return hop_collect_ct(client, watchlist, cfg, consensus, out.progress, out.event)


def run_recon(
    cfg: ReconConfig,
    on_status: Callable[[str], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_event: Callable[[ReconEvent], None] | None = None,
    rescan_requested: Callable[[], bool] | None = None,
) -> None:
    """Run the live recon loop. Blocking; interrupt with Ctrl+C.

    ``on_status`` and ``on_progress`` receive operator text; ``on_event``
    receives the same milestones as :data:`ReconEvent` values.
    ``rescan_requested`` is polled before every iteration; when it returns
    true, that iteration starts with an acquisition scan.
    """
    consensus = TimeConsensus(mission_precision_s=cfg.mission_precision_s)
    watchlist: list[StationCandidate] = []
    last_rescan_mono = float("-inf")
    iteration = 0

    if on_status is None:
        on_status = print
    out = _Outputs(on_status, on_progress, on_event)
    publish = out.publish

    # The handler also covers the connection and the first event, so that every
    # interruption ends the stream with recon_stopped.
    try:
        with RtlTcpClient(host=cfg.host, port=cfg.port) as client:
            info = client.info
            on_status(f"  rtl_tcp OK: tuner={info.tuner_type}, gains={info.gain_count}")
            publish(ReconStarted(tuner_type=info.tuner_type, gain_count=info.gain_count))
            while True:
                iteration += 1
                now_mono = time.monotonic()
                # Poll first, so that a pending request is consumed in any case.
                requested = rescan_requested is not None and rescan_requested()
                # ACQUISITION when requested, the watchlist is empty or rescan_min has elapsed.
                due = (now_mono - last_rescan_mono) > cfg.rescan_min * 60
                if requested or not watchlist or due:
                    watchlist = _acquire(client, cfg, iteration, out)
                    last_rescan_mono = time.monotonic()
                    on_status(f"  → watchlist: {[f'{s.freq_hz / 1e6:.2f}' for s in watchlist]}")
                    publish(WatchlistUpdated(iteration=iteration, watchlist=_detached(watchlist)))

                # MAINTENANCE — hop, or one WIDE capture
                new_obs = _maintain(client, cfg, iteration, watchlist, consensus, out)
                on_status(f"  collected {new_obs} new CT observations")

                # Display. Evaluate the consensus once: each evaluation updates trust.
                # Publish before the status text, which may block on a slow stderr,
                # so that the record is stamped close to the evaluation.
                now_mono = time.monotonic()
                next_rescan_in = max(0.0, cfg.rescan_min * 60 - (now_mono - last_rescan_mono))
                result = consensus.consensus(now_mono)
                publish(
                    CycleCompleted(
                        iteration=iteration,
                        new_observations=new_obs,
                        consensus=result,
                        watchlist=_detached(watchlist),
                        tracks=_track_snapshots(consensus, now_mono),
                        next_rescan_in_s=next_rescan_in,
                    )
                )
                on_status("\n" + render_status(consensus, watchlist, next_rescan_in, result=result))

                if cfg.iterations is not None and iteration >= cfg.iterations:
                    on_status(f"\n[#{iteration}] reached iterations={cfg.iterations}, stop")
                    publish(ReconStopped(iteration=iteration, reason="iterations"))
                    break
                if cfg.idle_s > 0:
                    time.sleep(cfg.idle_s)
    except KeyboardInterrupt:
        on_status("\nInterrupted by operator (Ctrl+C).")
        publish(ReconStopped(iteration=iteration, reason="interrupted"))


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
