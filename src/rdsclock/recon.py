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

import contextlib
import json
import math
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import ClassVar

import numpy as np

from .channelizer import ChannelDecodeResult, ChannelSpec, choose_center, decode_channels
from .ct_policy import CTInterpretation, CTPolicy
from .decoder import DecodeResult, decode_file, decode_iq
from .rds_clock import ClockTime
from .rtl_tcp import FreshCapture, RtlTcpAddress, RtlTcpClient
from .time_consensus import (
    ConsensusResult,
    StationFingerprint,
    StationObservation,
    TimeConsensus,
)


@dataclass
class StationCandidate:
    """A station discovered during a band scan.

    ``receiver`` is the ``host:port`` of the ``rtl_tcp`` receiver that found
    it, when known.
    """

    freq_hz: float
    rssi_db: float
    n_groups: int
    has_ct: bool
    pi: int | None
    ps_name: str = ""
    receiver: str | None = None


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
    # HOP with several receivers: each listed station with the receiver that
    # records it, None for host/port. When set, it defines hop_freqs_hz.
    hop_stations: tuple[tuple[RtlTcpAddress | None, float], ...] = ()
    # WIDE: when set, one capture records all these stations at once,
    # replacing the band scan and the hops.
    wide_freqs_hz: tuple[float, ...] = ()
    wide_center_hz: float | None = None  # None: chosen by wide_center()
    wide_sample_rate: int = WIDE_SAMPLE_RATE
    # How to read each station's CT "UTC" field; by default as the standard says.
    ct_policy: CTPolicy = field(default_factory=CTPolicy)

    def __post_init__(self) -> None:
        if self.hop_stations:
            self.hop_freqs_hz = tuple(freq_hz for _, freq_hz in self.hop_stations)

    def default_receiver(self) -> RtlTcpAddress:
        """The receiver at ``host``/``port``."""
        return RtlTcpAddress(self.host, self.port)

    def hop_receivers(self) -> dict[RtlTcpAddress, tuple[float, ...]]:
        """The listed HOP stations of every receiver, in the order first listed.

        Empty without a HOP list; stations without a receiver go to
        :meth:`default_receiver`.
        """
        if not self.hop_stations:
            return {self.default_receiver(): self.hop_freqs_hz} if self.hop_freqs_hz else {}
        plan: dict[RtlTcpAddress, tuple[float, ...]] = {}
        for address, freq_hz in self.hop_stations:
            key = address if address is not None else self.default_receiver()
            plan[key] = (*plan.get(key, ()), freq_hz)
        return plan

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
    """``rtl_tcp`` answered; the loop is about to start.

    ``ct_timezone_rules`` lists the ``--ct-timezone`` rules in effect, each in
    a form that parses back to it; it is empty when every station is read as
    the standard says.

    ``receivers`` lists every receiver of the run as ``host:port``. With
    several, the tuner fields describe the first one that answered; the
    others that did not answer follow as :class:`ReceiverFailed`.
    """

    kind: ClassVar[str] = "recon_started"
    tuner_type: int
    gain_count: int
    ct_timezone_rules: tuple[str, ...] = ()
    receivers: tuple[str, ...] = ()


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
    """One watchlist station recorded and decoded; ``clock_time`` is ``None`` without CT.

    ``queued_s`` is how long the capture's first sample had queued when the
    capture began reading (:attr:`FreshCapture.queued_s`); receive times
    account for it. ``caught_up`` is false when the capture never caught up
    with real time: its receive times are then only upper bounds, and its CT
    stays out of the consensus.

    ``clock_time`` is the CT as decoded. ``ct_interpretation`` is how
    :attr:`ReconConfig.ct_policy` read it: the effective UTC that the
    consensus sees, or ``None`` when it could not be mapped to one instant.
    In JSON Lines its fields are merged into the ``clock_time`` object as
    ``effective_utc``, ``ct_timezone`` and ``ct_status``.

    ``receiver`` is the ``host:port`` of the receiver that recorded it.
    """

    kind: ClassVar[str] = "hop_result"
    freq_hz: float
    pi: int | None
    ps_name: str
    rssi_db: float
    n_groups: int
    n_groups_clean: int
    n_groups_corrected: int
    clock_time: ClockTime | None
    queued_s: float = 0.0
    caught_up: bool = True
    ct_interpretation: CTInterpretation | None = None
    receiver: str | None = None

    @property
    def effective_ct(self) -> CTInterpretation | None:
        """How the CT was read; standard UTC when no interpretation was recorded."""
        if self.clock_time is None:
            return None
        if self.ct_interpretation is not None:
            return self.ct_interpretation
        return CTPolicy().interpret(self.clock_time, self.pi, self.freq_hz)


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


@dataclass(frozen=True)
class ReceiverFailed:
    """One of several receivers could not connect, or its connection failed.

    Only a run with several receivers reports this; the others go on, its
    stations leave the watchlist, and the next cycle connects to it again.
    When every receiver has failed, the run ends with the error instead, as
    a run with one receiver does.
    """

    kind: ClassVar[str] = "receiver_failed"
    iteration: int
    receiver: str
    error: str


ReconEvent = (
    ReconStarted
    | AcquisitionStarted
    | ScanStep
    | WatchlistUpdated
    | MaintenanceStarted
    | HopResult
    | CycleCompleted
    | ReconStopped
    | ReceiverFailed
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
    if isinstance(event, HopResult):
        _merge_ct_interpretation(record, event)
    return json.dumps(_jsonable(record), allow_nan=False, separators=(",", ":"))


def _merge_ct_interpretation(record: dict[str, object], hop: HopResult) -> None:
    """Describe the CT reading inside ``clock_time``: additive keys, existing ones unchanged."""
    del record["ct_interpretation"]
    interpretation = hop.effective_ct
    clock_time = record["clock_time"]
    if interpretation is not None and isinstance(clock_time, dict):
        clock_time["effective_utc"] = interpretation.utc
        clock_time["ct_timezone"] = interpretation.zone
        clock_time["ct_status"] = interpretation.status


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
    receiver: str | None = None,
) -> list[StationCandidate]:
    """Quickly scan the FM band. Per frequency: capture ``scan_dwell_s``,
    measure RSSI, and attempt a decode to check for RDS / Clock-Time.

    With ``cfg.hop_freqs_hz`` only the listed stations are checked, and the
    RSSI threshold does not apply to them. ``on_event`` receives a
    :class:`ScanStep` for every frequency, including those that were not
    decoded because of the RSSI threshold. ``receiver`` names ``client`` in
    the candidates."""

    def emit(msg: str) -> None:
        if progress:
            progress(msg)

    client.configure_fm(cfg.fs_scan, gain_db=cfg.gain_db)

    candidates: list[StationCandidate] = []
    for freq_hz in _scan_tunings_hz(cfg):
        client.set_frequency(int(freq_hz))
        capture = client.capture_fresh(cfg.scan_dwell_s, cfg.fs_scan, settle_s=cfg.settle_s)
        _report_backlog(f"scan {freq_hz / 1e6:6.2f} MHz", capture, progress)
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
                receiver=receiver,
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


@dataclass(frozen=True)
class _CaptureStart:
    """A capture's first sample on the monotonic and wall clocks, and how it was dated."""

    monotonic_ns: int
    wall: datetime
    queued_s: float
    caught_up: bool


def _capture_start(capture: FreshCapture) -> _CaptureStart:
    """Map a capture's host monotonic start onto wall time.

    Call it right after the capture, so that neither the duration of the
    capture nor the decoding that follows enters the CT anchor.
    """
    wall_anchor = datetime.now(UTC)
    monotonic_anchor = time.monotonic_ns()
    start_ns = capture.capture_start_monotonic_ns
    wall = wall_anchor - timedelta(seconds=(monotonic_anchor - start_ns) / 1e9)
    return _CaptureStart(start_ns, wall, capture.queued_s, capture.caught_up)


def _report_backlog(
    label: str, capture: FreshCapture, progress: Callable[[str], None] | None
) -> None:
    """Tell the operator that a capture began with samples queued while not reading."""
    if not progress:
        return
    if not capture.caught_up:
        progress(
            f"  {label}: the capture began at least {capture.queued_s:.1f} s behind real "
            "time and never caught up; its Clock-Time stays out of the consensus"
        )
    elif capture.backlogged:
        progress(
            f"  {label}: the capture began {capture.queued_s:.1f} s behind real time "
            "(queued samples); receive times account for it"
        )


def _record_station(
    freq_hz: float,
    result: DecodeResult,
    rssi_db: float,
    capture_start: _CaptureStart,
    consensus: TimeConsensus,
    ct_policy: CTPolicy | None = None,
    receiver: str | None = None,
) -> HopResult:
    """Store a decoded station's CT in the consensus and describe the station.

    ``ct_policy`` decides how the CT's UTC field is read; only the effective
    UTC enters the consensus. A capture that never caught up with real time
    has no known receive times, and a local time that maps to no single UTC
    instant has no effective UTC: such CT is described but not stored.
    """
    ct = result.info.latest_clock
    policy = ct_policy if ct_policy is not None else CTPolicy()
    interpretation = None if ct is None else policy.interpret(ct, result.info.pi, freq_hz)
    effective_utc = None if interpretation is None else interpretation.utc
    if ct is not None and effective_utc is not None and capture_start.caught_up:
        received_wall = None
        if ct.rx_monotonic_ns is not None:
            received_wall = capture_start.wall + timedelta(
                seconds=(ct.rx_monotonic_ns - capture_start.monotonic_ns) / 1_000_000_000
            )
        fingerprint = StationFingerprint(
            pi_code=result.info.pi,
            cfo_hz=None,  # the decoder currently does not measure RF CFO
            rssi_db=rssi_db,
        )
        obs = StationObservation(
            freq_hz=freq_hz,
            pi=result.info.pi,
            ct_utc=effective_utc,
            received_monotonic=(
                ct.rx_monotonic_ns / 1e9
                if ct.rx_monotonic_ns is not None
                else capture_start.monotonic_ns / 1e9
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
        queued_s=capture_start.queued_s,
        caught_up=capture_start.caught_up,
        ct_interpretation=interpretation,
        receiver=receiver,
    )


def _entered_consensus(hop: HopResult) -> bool:
    """Whether :func:`_record_station` stored the CT of ``hop`` in the consensus."""
    interpretation = hop.effective_ct
    return interpretation is not None and interpretation.utc is not None and hop.caught_up


def _report_station(
    mode: str,
    hop: HopResult,
    progress: Callable[[str], None] | None,
    on_event: Callable[[ReconEvent], None] | None,
) -> int:
    """Pass one station's result on; return 1 when its CT entered the consensus, otherwise 0."""
    interpretation = hop.effective_ct
    usable = interpretation is not None and interpretation.utc is not None
    if progress:
        if hop.clock_time is not None and interpretation is not None:
            if not hop.caught_up:
                unused = " (receive time unknown, not used)"
            elif not usable:
                unused = " (no single UTC instant, not used)"
            else:
                unused = ""
            progress(
                f"  {mode} {hop.freq_hz / 1e6:6.2f} MHz: "
                f"CT={_ct_text(hop.clock_time, interpretation)} groups={hop.n_groups}{unused}"
            )
        else:
            progress(f"  {mode} {hop.freq_hz / 1e6:6.2f} MHz: no CT (groups={hop.n_groups})")
    if on_event:
        on_event(hop)
    return 1 if _entered_consensus(hop) else 0


def _ct_text(ct: ClockTime, interpretation: CTInterpretation) -> str:
    """The CT for operator text: as broadcast, plus the effective UTC when read as local time."""
    if interpretation.status == "standard":
        return f"{ct.utc.strftime('%H:%M:%S')}Z"
    local = f"{ct.utc.strftime('%H:%M:%S')} {interpretation.zone}"
    if interpretation.utc is None:
        return f"{local} ({interpretation.status})"
    return f"{local} → {interpretation.utc.strftime('%H:%M:%S')}Z"


def hop_collect_ct(
    client: RtlTcpClient,
    watchlist: list[StationCandidate],
    cfg: ReconConfig,
    consensus: TimeConsensus,
    progress: Callable[[str], None] | None = None,
    on_event: Callable[[ReconEvent], None] | None = None,
    receiver: str | None = None,
    lock: contextlib.AbstractContextManager | None = None,
) -> int:
    """Hop through the watchlist: tune, record, decode, store CT in the consensus.

    Returns the number of new CT observations recorded in this iteration.
    ``on_event`` receives a :class:`HopResult` for every station, with or
    without CT. ``receiver`` names ``client`` in them. When other receivers
    hop at the same time, ``lock`` is held while a result enters the
    consensus and is reported, but not while recording or decoding.
    """
    guard = lock if lock is not None else contextlib.nullcontext()
    client.configure_fm(cfg.sample_rate, gain_db=cfg.gain_db)

    n_obs = 0
    for station in watchlist:
        client.set_frequency(int(station.freq_hz))
        capture = client.capture_fresh(cfg.dwell_s, cfg.sample_rate, settle_s=cfg.settle_s)
        capture_start = _capture_start(capture)
        _report_backlog(f"hop {station.freq_hz / 1e6:6.2f} MHz", capture, progress)
        iq = capture.iq
        result = decode_iq(
            iq,
            fs=cfg.sample_rate,
            capture_start_monotonic_ns=capture_start.monotonic_ns,
        )
        rssi_db = float(10 * np.log10(float(np.mean(np.abs(iq) ** 2)) + 1e-12))
        with guard:
            hop = _record_station(
                station.freq_hz, result, rssi_db, capture_start, consensus, cfg.ct_policy, receiver
            )
            n_obs += _report_station("hop", hop, progress, on_event)
    return n_obs


def _wide_capture(
    client: RtlTcpClient,
    cfg: ReconConfig,
    freqs_hz: Sequence[float],
    duration_s: float,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[ChannelDecodeResult], _CaptureStart]:
    """Record ``duration_s`` around the WIDE centre and decode ``freqs_hz`` from it.

    Returns the results per station and the capture's start.
    """
    center_hz = cfg.wide_center()
    client.configure_fm(cfg.wide_sample_rate, gain_db=cfg.gain_db)
    client.set_frequency(int(center_hz))
    capture = client.capture_fresh(duration_s, cfg.wide_sample_rate, settle_s=cfg.settle_s)
    capture_start = _capture_start(capture)
    _report_backlog("wide", capture, progress)
    # One station at a time: each extraction holds a copy of the capture.
    results = decode_channels(
        iq_wide=capture.iq,
        fs_wide=cfg.wide_sample_rate,
        f_center=center_hz,
        channels=[ChannelSpec(freq_hz=freq_hz) for freq_hz in freqs_hz],
        max_workers=1,
        capture_start_monotonic_ns=capture_start.monotonic_ns,
    )
    return results, capture_start


def wide_scan(
    client: RtlTcpClient,
    cfg: ReconConfig,
    progress: Callable[[str], None] | None = None,
    on_event: Callable[[ReconEvent], None] | None = None,
    receiver: str | None = None,
) -> list[StationCandidate]:
    """WIDE acquisition: decode every station of ``cfg.wide_freqs_hz`` from one
    ``scan_dwell_s`` capture instead of scanning the band.

    ``on_event`` receives a :class:`ScanStep` for every station. The RSSI
    threshold does not apply: the capture contains every station anyway.
    ``receiver`` names ``client`` in the candidates.
    """
    results, _ = _wide_capture(client, cfg, cfg.wide_freqs_hz, cfg.scan_dwell_s, progress)
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
            receiver=receiver,
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
    receiver: str | None = None,
) -> int:
    """WIDE maintenance: record every watchlist station from one ``dwell_s``
    capture and store their CT in the consensus.

    Returns the number of new CT observations. ``on_event`` receives a
    :class:`HopResult` for every station, as after a hop. An empty watchlist
    records nothing. ``receiver`` names ``client`` in the results.
    """
    if not watchlist:
        return 0
    freqs_hz = [station.freq_hz for station in watchlist]
    results, capture_start = _wide_capture(client, cfg, freqs_hz, cfg.dwell_s, progress)
    n_obs = 0
    for channel in results:
        hop = _record_station(
            channel.spec.freq_hz,
            channel.result,
            channel.rssi_db,
            capture_start,
            consensus,
            cfg.ct_policy,
            receiver,
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


def _scan_listed(
    client: RtlTcpClient, cfg: ReconConfig, out: _Outputs, receiver: str | None
) -> list[StationCandidate]:
    """Check the listed HOP stations of ``cfg``; return the next watchlist."""
    ranked = rank_candidates(quick_scan_band(client, cfg, out.progress, out.event, receiver))
    # The operator chose these stations: keep every one with RDS, uncapped.
    return [c for c in ranked if c.n_groups > 0]


def _acquire(
    client: RtlTcpClient,
    cfg: ReconConfig,
    iteration: int,
    out: _Outputs,
    receiver: str | None = None,
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
        ranked = rank_candidates(wide_scan(client, cfg, out.progress, out.event, receiver))
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
        return _scan_listed(client, cfg, out, receiver)

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
        quick_scan_band(client, cfg, progress=out.progress, on_event=out.event, receiver=receiver)
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
    receiver: str | None = None,
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
        return wide_collect_ct(client, watchlist, cfg, consensus, out.progress, out.event, receiver)

    out.status(f"\n[#{iteration}] MAINTENANCE (hop {len(watchlist)} stations)…")
    out.publish(MaintenanceStarted(iteration=iteration, n_stations=len(watchlist)))
    return hop_collect_ct(
        client, watchlist, cfg, consensus, out.progress, out.event, receiver=receiver
    )


def _complete_cycle(
    cfg: ReconConfig,
    consensus: TimeConsensus,
    iteration: int,
    new_obs: int,
    watchlist: list[StationCandidate],
    last_rescan_mono: float,
    out: _Outputs,
) -> bool:
    """Report the end of a cycle; return whether the run has reached its iterations."""
    out.status(f"  collected {new_obs} new CT observations")

    # Display. Evaluate the consensus once: each evaluation updates trust.
    # Publish before the status text, which may block on a slow stderr,
    # so that the record is stamped close to the evaluation.
    now_mono = time.monotonic()
    next_rescan_in = max(0.0, cfg.rescan_min * 60 - (now_mono - last_rescan_mono))
    result = consensus.consensus(now_mono)
    out.publish(
        CycleCompleted(
            iteration=iteration,
            new_observations=new_obs,
            consensus=result,
            watchlist=_detached(watchlist),
            tracks=_track_snapshots(consensus, now_mono),
            next_rescan_in_s=next_rescan_in,
        )
    )
    out.status("\n" + render_status(consensus, watchlist, next_rescan_in, result=result))

    if cfg.iterations is not None and iteration >= cfg.iterations:
        out.status(f"\n[#{iteration}] reached iterations={cfg.iterations}, stop")
        out.publish(ReconStopped(iteration=iteration, reason="iterations"))
        return True
    if cfg.idle_s > 0:
        time.sleep(cfg.idle_s)
    return False


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

    When :attr:`ReconConfig.hop_stations` spreads the HOP stations over
    several receivers, every receiver hops through its own stations in its
    own thread (see :class:`_MultiReceiverRun`). Callbacks are then called
    from those threads too, but never two at a time.
    """
    if on_status is None:
        on_status = print
    out = _Outputs(on_status, on_progress, on_event)
    receivers = cfg.hop_receivers()
    if len(receivers) > 1:
        _MultiReceiverRun(cfg, receivers, out).run(rescan_requested)
        return
    address = next(iter(receivers), cfg.default_receiver())
    if address != cfg.default_receiver():
        cfg = replace(cfg, host=address.host, port=address.port)
    receiver = str(address)

    consensus = TimeConsensus(mission_precision_s=cfg.mission_precision_s)
    watchlist: list[StationCandidate] = []
    last_rescan_mono = float("-inf")
    iteration = 0
    publish = out.publish

    # The handler also covers the connection and the first event, so that every
    # interruption ends the stream with recon_stopped.
    try:
        with RtlTcpClient(host=cfg.host, port=cfg.port) as client:
            info = client.info
            on_status(f"  rtl_tcp OK: tuner={info.tuner_type}, gains={info.gain_count}")
            publish(
                ReconStarted(
                    tuner_type=info.tuner_type,
                    gain_count=info.gain_count,
                    ct_timezone_rules=tuple(str(rule) for rule in cfg.ct_policy.rules),
                    receivers=(receiver,),
                )
            )
            while True:
                iteration += 1
                now_mono = time.monotonic()
                # Poll first, so that a pending request is consumed in any case.
                requested = rescan_requested is not None and rescan_requested()
                # ACQUISITION when requested, the watchlist is empty or rescan_min has elapsed.
                due = (now_mono - last_rescan_mono) > cfg.rescan_min * 60
                if requested or not watchlist or due:
                    watchlist = _acquire(client, cfg, iteration, out, receiver)
                    last_rescan_mono = time.monotonic()
                    on_status(f"  → watchlist: {[f'{s.freq_hz / 1e6:.2f}' for s in watchlist]}")
                    publish(WatchlistUpdated(iteration=iteration, watchlist=_detached(watchlist)))

                # MAINTENANCE — hop, or one WIDE capture
                new_obs = _maintain(client, cfg, iteration, watchlist, consensus, out, receiver)
                if _complete_cycle(
                    cfg, consensus, iteration, new_obs, watchlist, last_rescan_mono, out
                ):
                    break
    except KeyboardInterrupt:
        on_status("\nInterrupted by operator (Ctrl+C).")
        publish(ReconStopped(iteration=iteration, reason="interrupted"))


# ----------------------- SEVERAL RECEIVERS -----------------------

# How long a stopping run waits for each receiver thread to notice.
_THREAD_STOP_TIMEOUT_S = 2.0


class _OutputError(Exception):
    """An output callback failed in a receiver thread; ``__cause__`` is its error.

    Kept apart from ``OSError`` so that a failed write is never mistaken
    for a failed receiver.
    """


class _SharedOutputs:
    """:class:`_Outputs` for several threads: one line or record at a time.

    ``lock`` also serialises the consensus updates. After :meth:`close`,
    output from the receiver threads is dropped, so that nothing follows
    ``recon_stopped``.
    """

    def __init__(self, out: _Outputs) -> None:
        self.lock = threading.RLock()
        self._out = out
        self._closed = False

    def close(self) -> None:
        with self.lock:
            self._closed = True

    def _call(self, callback: Callable, value: object, in_worker: bool) -> None:
        with self.lock:
            if self._closed:
                return
            if not in_worker:
                callback(value)
                return
            try:
                callback(value)
            except Exception as exc:
                raise _OutputError from exc

    def _wrap(self, callback: Callable | None, in_worker: bool) -> Callable | None:
        if callback is None:
            return None
        return lambda value: self._call(callback, value, in_worker)

    def main(self) -> _Outputs:
        """Outputs for the thread that runs the loop."""
        return _Outputs(
            self._wrap(self._out.status, False),
            self._wrap(self._out.progress, False),
            self._wrap(self._out.event, False),
        )

    def worker(self, receiver: str, on_hop: Callable[[HopResult], None]) -> _Outputs:
        """Outputs for the thread of ``receiver``: progress names it, hops go to ``on_hop``."""
        progress = self._out.progress
        if progress is not None:
            inner = progress
            progress = lambda message: inner(f"  [{receiver}] {message.lstrip()}")  # noqa: E731
        forward = self._wrap(self._out.event, True)

        def event(value: ReconEvent) -> None:
            if isinstance(value, HopResult):
                on_hop(value)
            if forward is not None:
                forward(value)

        return _Outputs(self._wrap(self._out.status, True), self._wrap(progress, True), event)


@dataclass
class _ReceiverSlot:
    """One receiver of a run with several, and its part of the watchlist."""

    address: RtlTcpAddress
    cfg: ReconConfig  # the run's settings, with only this receiver's stations
    client: RtlTcpClient | None = None
    watchlist: list[StationCandidate] = field(default_factory=list)
    new_observations: int = 0

    @property
    def name(self) -> str:
        return str(self.address)


def _error_text(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


class _MultiReceiverRun:
    """The live loop of :func:`run_recon` with several receivers.

    The cycle is the same as with one receiver: an acquisition when one is
    due, then one maintenance pass and the consensus. Within each phase every
    receiver records its own stations in its own thread, and the phase ends
    when all of them have finished. A receiver that fails is reported as
    :class:`ReceiverFailed`, leaves the watchlist with its stations, and is
    connected again at the start of the next cycle; an acquisition then
    checks its stations again. When no receiver is left, the latest error
    ends the run, as it does with one receiver.
    """

    def __init__(
        self, cfg: ReconConfig, receivers: dict[RtlTcpAddress, tuple[float, ...]], out: _Outputs
    ) -> None:
        self.cfg = cfg
        self.base_out = out
        self.shared = _SharedOutputs(out)
        self.out = self.shared.main()
        self.consensus = TimeConsensus(mission_precision_s=cfg.mission_precision_s)
        self.slots = [
            _ReceiverSlot(
                address,
                replace(
                    cfg, host=address.host, port=address.port, hop_stations=(), hop_freqs_hz=freqs
                ),
            )
            for address, freqs in receivers.items()
        ]
        self.iteration = 0
        self.threads: list[threading.Thread] = []
        self.last_error: OSError | None = None

    def run(self, rescan_requested: Callable[[], bool] | None) -> None:
        try:
            self._loop(rescan_requested)
        except KeyboardInterrupt:
            self._stop()
            self.base_out.status("\nInterrupted by operator (Ctrl+C).")
            self.base_out.publish(ReconStopped(iteration=self.iteration, reason="interrupted"))
        finally:
            self._stop()

    def _loop(self, rescan_requested: Callable[[], bool] | None) -> None:
        cfg, out = self.cfg, self.out
        failures = self._in_parallel(self.slots, self._connect)
        self._require_a_receiver(failures)
        first = next(slot for slot in self.slots if slot.client is not None)
        for slot in self.slots:
            if slot.client is not None:
                info = slot.client.info
                out.status(
                    f"  rtl_tcp OK: {slot.name} tuner={info.tuner_type}, gains={info.gain_count}"
                )
        out.publish(
            ReconStarted(
                tuner_type=first.client.info.tuner_type,
                gain_count=first.client.info.gain_count,
                ct_timezone_rules=tuple(str(rule) for rule in cfg.ct_policy.rules),
                receivers=tuple(slot.name for slot in self.slots),
            )
        )
        self._report(failures)
        last_rescan_mono = float("-inf")
        while True:
            self.iteration += 1
            now_mono = time.monotonic()
            # Poll first, so that a pending request is consumed in any case.
            requested = rescan_requested is not None and rescan_requested()
            down = [slot for slot in self.slots if slot.client is None]
            if down:
                failures = self._in_parallel(down, self._connect)
                for slot in down:
                    if slot.client is not None:
                        out.status(f"  receiver {slot.name} connected again")
                self._report(failures)
                self._require_a_receiver(failures)
            up = [slot for slot in self.slots if slot.client is not None]
            due = (now_mono - last_rescan_mono) > cfg.rescan_min * 60
            everywhere = requested or due or not any(slot.watchlist for slot in up)
            # A receiver that has just connected again checks its stations on its own.
            acquiring = up if everywhere else [slot for slot in up if not slot.watchlist]
            if acquiring:
                self._acquire(acquiring)
                if everywhere:
                    last_rescan_mono = time.monotonic()
                watchlist = self._watchlist()
                out.status(f"  → watchlist: {[f'{s.freq_hz / 1e6:.2f}' for s in watchlist]}")
                out.publish(
                    WatchlistUpdated(iteration=self.iteration, watchlist=_detached(watchlist))
                )
            new_obs = self._maintain()
            if _complete_cycle(
                cfg,
                self.consensus,
                self.iteration,
                new_obs,
                self._watchlist(),
                last_rescan_mono,
                out,
            ):
                break

    def _watchlist(self) -> list[StationCandidate]:
        return [station for slot in self.slots for station in slot.watchlist]

    def _connect(self, slot: _ReceiverSlot) -> None:
        client = RtlTcpClient(host=slot.address.host, port=slot.address.port)
        client.connect()
        slot.client = client

    def _acquire(self, slots: list[_ReceiverSlot]) -> None:
        freqs_hz = [freq_hz for slot in slots for freq_hz in slot.cfg.hop_freqs_hz]
        self.out.status(
            f"\n[#{self.iteration}] ACQUISITION (check {len(freqs_hz)} listed stations "
            f"on {len(slots)} receivers)…"
        )
        self.out.publish(
            AcquisitionStarted(
                iteration=self.iteration,
                band_start_mhz=min(freqs_hz) / 1e6,
                band_end_mhz=max(freqs_hz) / 1e6,
                scan_step_mhz=0.0,
                n_points=len(freqs_hz),
            )
        )

        def scan(slot: _ReceiverSlot) -> None:
            out = self.shared.worker(slot.name, lambda hop: None)
            slot.watchlist = _scan_listed(slot.client, slot.cfg, out, slot.name)

        self._handle(self._in_parallel(slots, scan))

    def _maintain(self) -> int:
        slots = [slot for slot in self.slots if slot.client is not None and slot.watchlist]
        watchlist = self._watchlist()
        self.out.status(
            f"\n[#{self.iteration}] MAINTENANCE (hop {len(watchlist)} stations "
            f"on {len(slots)} receivers)…"
        )
        self.out.publish(MaintenanceStarted(iteration=self.iteration, n_stations=len(watchlist)))

        def hop(slot: _ReceiverSlot) -> None:
            slot.new_observations = 0

            # Count as the results arrive, so that a receiver that fails
            # midway still counts what it added to the consensus.
            def count(result: HopResult) -> None:
                slot.new_observations += _entered_consensus(result)

            out = self.shared.worker(slot.name, count)
            hop_collect_ct(
                slot.client,
                slot.watchlist,
                slot.cfg,
                self.consensus,
                out.progress,
                out.event,
                receiver=slot.name,
                lock=self.shared.lock,
            )

        self._handle(self._in_parallel(slots, hop))
        return sum(slot.new_observations for slot in slots)

    def _in_parallel(
        self, slots: list[_ReceiverSlot], job: Callable[[_ReceiverSlot], None]
    ) -> list[tuple[_ReceiverSlot, OSError]]:
        """Run ``job`` for every slot in a thread of its own and wait for all of them.

        Returns the receivers that failed, with their errors. Any other error,
        such as one of an output callback, is raised once every thread ended.
        """
        failures: dict[int, OSError] = {}
        errors: list[BaseException] = []

        def run(index: int, slot: _ReceiverSlot) -> None:
            try:
                job(slot)
            except _OutputError as exc:
                errors.append(exc.__cause__)
            except OSError as exc:
                failures[index] = exc
            except BaseException as exc:
                errors.append(exc)

        self.threads = [
            threading.Thread(
                target=run, args=(index, slot), name=f"rdsclock {slot.name}", daemon=True
            )
            for index, slot in enumerate(slots)
        ]
        for thread in self.threads:
            thread.start()
        for thread in self.threads:
            thread.join()
        if errors:
            raise errors[0]
        return [(slots[index], failures[index]) for index in sorted(failures)]

    def _handle(self, failures: list[tuple[_ReceiverSlot, OSError]]) -> None:
        self._report(failures)
        self._require_a_receiver(failures)

    def _report(self, failures: list[tuple[_ReceiverSlot, OSError]]) -> None:
        for slot, exc in failures:
            self._disconnect(slot)
            slot.watchlist = []
            self.last_error = exc
            self.out.status(
                f"  receiver {slot.name} failed: {_error_text(exc)}; reconnecting next cycle"
            )
            self.out.publish(
                ReceiverFailed(iteration=self.iteration, receiver=slot.name, error=_error_text(exc))
            )

    def _require_a_receiver(self, failures: list[tuple[_ReceiverSlot, OSError]]) -> None:
        if all(slot.client is None for slot in self.slots):
            raise failures[-1][1] if failures else (self.last_error or OSError("no receiver"))

    @staticmethod
    def _disconnect(slot: _ReceiverSlot) -> None:
        client, slot.client = slot.client, None
        if client is not None:
            # A failed capture may have closed it in its own thread already.
            with contextlib.suppress(OSError, AttributeError):
                client.close()

    def _stop(self) -> None:
        """Drop late output, wake every receiver thread and close the receivers."""
        self.shared.close()
        for slot in self.slots:
            if slot.client is not None:
                slot.client.interrupt()
        for thread in self.threads:
            thread.join(_THREAD_STOP_TIMEOUT_S)
        for slot in self.slots:
            self._disconnect(slot)


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
        if ct is None:
            on_status(
                f"  [{i}/{len(files)}] {freq_mhz:.2f} MHz: no CT "
                f"(groups={result.n_groups}, PS='{result.info.ps_name}')"
            )
        else:
            interpretation = cfg.ct_policy.interpret(ct, result.info.pi, freq_mhz * 1e6)
            unused = ""
            if interpretation.utc is None:
                unused = " (no single UTC instant, not used)"
            else:
                obs = StationObservation(
                    freq_hz=freq_mhz * 1e6,
                    pi=result.info.pi,
                    ct_utc=interpretation.utc,
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
                f"PS='{result.info.ps_name}' CT={_ct_text(ct, interpretation)} "
                f"groups={result.n_groups}{unused}"
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
