"""Multi-source time consensus + per-station trust scoring + holdover.

Design rationale (GPS-denied passive-RF operation):
    - Maintain a watchlist of FM stations together with a trust score.
    - For each station, observed Clock-Time is translated into an
      "offset relative to the local monotonic clock".
    - The consensus time is the median of those offsets across N ≥ 2
      stations.
    - When a station diverges from the median, its trust score drops
      (Hampel outlier rule).
    - Holdover: between successive CT messages we extrapolate UTC from
      the local monotonic clock, adjusted for an estimated ppm drift.
      Uncertainty grows linearly with the age of the most recent CT.
    - Operator display: ``UTC HH:MM:SS  ±Xs  N=4  trust=HIGH``.
"""

import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum

from .rds_clock import ClockTime


class TrustLevel(Enum):
    HIGH = "HIGH"  # ≥3 stations agree on a recent CT (<2 min old)
    MEDIUM = "MEDIUM"  # 2 stations, or a fresh single source
    LOW = "LOW"  # 1 station, or stale (>5 min)
    STALE = "STALE"  # holdover beyond the mission precision threshold


# Outlier threshold: a station whose CT diverges from the median by more
# than this number of seconds is penalised.
#
# RDS Group 4A only carries hour and minute — there are no seconds in the
# protocol. A "minute" only ticks over when the station's internal clock
# crosses the boundary, and different stations broadcast Group 4A at
# different phases (one may emit CT shortly after :00, another shortly
# before :30, etc.). Two well-synchronised stations observed at the same
# wall-clock moment can therefore differ by up to ~60 s purely because of
# this protocol granularity. The estimator extrapolates each observation
# with the local monotonic clock, so the practical sub-minute spread is
# normally well below 60 s, but to give honest stations breathing room
# across the minute boundary we add a half-step safety margin (45 s for
# the protocol step plus 45 s for inter-station phase) → 90 s.
OUTLIER_THRESHOLD_S = 90.0

# Stale threshold: CT older than this is treated as obsolete.
STALE_AGE_S = 300.0

# Penalty applied per consecutive outlier observation.
OUTLIER_PENALTY = 0.4

# Bonus applied when a station agrees with the median.
AGREEMENT_BONUS = 0.05

# Default oscillator ppm used to grow uncertainty with holdover age.
DEFAULT_LOCAL_OSC_PPM = 50.0  # typical RTL-SDR crystal: 20–50 ppm
MINUTE_NS = 60_000_000_000


@dataclass
class StationFingerprint:
    """Raw RF features used for anti-spoof checks. All fields optional."""

    pi_code: int | None = None
    cfo_hz: float | None = None  # actual carrier offset vs 57 kHz
    rssi_db: float | None = None  # IQ power
    group_4a_rate_hz: float | None = None  # rate of Group 4A frames
    rds_to_audio_ratio: float | None = None


@dataclass
class StationObservation:
    """A single Clock-Time observation from a station."""

    freq_hz: float
    pi: int | None
    ct_utc: datetime
    received_monotonic: float  # monotonic clock at the moment of reception (s)
    fingerprint: StationFingerprint = field(default_factory=StationFingerprint)
    clock_time: ClockTime | None = None
    rx_monotonic_ns: int | None = None
    received_wall_utc: datetime | None = None
    tx_latency_ns: int | None = None

    def __post_init__(self) -> None:
        if self.clock_time is not None and self.rx_monotonic_ns is None:
            self.rx_monotonic_ns = self.clock_time.rx_monotonic_ns
        if self.received_wall_utc is not None and self.received_wall_utc.tzinfo is None:
            self.received_wall_utc = self.received_wall_utc.replace(tzinfo=UTC)


@dataclass
class StationTrack:
    """Per-transmitter state, keyed by PI and frequency bin."""

    freq_hz: float
    pi: int | None
    observations: list[StationObservation] = field(default_factory=list)
    trust_score: float = 0.5  # in [0..1], starts neutral
    consecutive_outliers: int = 0
    fingerprint_baseline: StationFingerprint | None = None
    tx_latency_ns: int | None = None

    @property
    def label(self) -> str:
        pi_str = f"PI=0x{self.pi:04X}" if self.pi is not None else "PI=?"
        return f"{self.freq_hz / 1e6:.2f}MHz {pi_str}"

    @property
    def last(self) -> StationObservation | None:
        return self.observations[-1] if self.observations else None

    def estimated_utc_now(self, monotonic_now: float) -> datetime | None:
        """Extrapolate UTC from the last CT observation plus elapsed monotonic time."""
        if not self.observations:
            return None
        last = self.observations[-1]
        dt = monotonic_now - last.received_monotonic
        return last.ct_utc + timedelta(seconds=dt)

    def age_s(self, monotonic_now: float) -> float:
        if not self.observations:
            return float("inf")
        return monotonic_now - self.observations[-1].received_monotonic

    def add_observation(self, obs: StationObservation) -> None:
        self.observations.append(obs)
        # Trim to the most recent 30 observations to bound memory use.
        if len(self.observations) > 30:
            self.observations = self.observations[-30:]

    def is_active(self, monotonic_now: float, max_age_s: float = STALE_AGE_S) -> bool:
        return self.age_s(monotonic_now) <= max_age_s


@dataclass
class ConsensusResult:
    """Aggregated time result across all active stations."""

    utc: datetime | None
    uncertainty_s: float
    trust_level: TrustLevel
    n_sources: int
    contributing_freqs_mhz: list[float]
    outlier_freqs_mhz: list[float]
    median_age_s: float
    notes: str = ""

    def format_display(self) -> str:
        if self.utc is None:
            return f"TIME UNAVAILABLE  trust={self.trust_level.value}  {self.notes}"
        utc_str = self.utc.strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"UTC {utc_str}  ±{self.uncertainty_s:.0f}s  "
            f"N={self.n_sources}  trust={self.trust_level.value}  "
            f"age={self.median_age_s:.0f}s"
        )


@dataclass(frozen=True)
class SubSecondEstimate:
    """UTC estimate built from receive timestamps and station latency phases."""

    utc: datetime
    precision_ms: float
    station_count: int


_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _empty_consensus_result() -> "ConsensusResult":
    return ConsensusResult(
        utc=None,
        uncertainty_s=float("inf"),
        trust_level=TrustLevel.STALE,
        n_sources=0,
        contributing_freqs_mhz=[],
        outlier_freqs_mhz=[],
        median_age_s=float("inf"),
        notes="no active sources",
    )


def _collect_estimates(
    active: list["StationTrack"], monotonic_now: float
) -> list[tuple["StationTrack", datetime]]:
    estimates: list[tuple[StationTrack, datetime]] = []
    for t in active:
        est = t.estimated_utc_now(monotonic_now)
        if est is not None:
            estimates.append((t, est))
    return estimates


def _median_and_mad(epochs: list[float]) -> tuple[float, float]:
    """Return (median, median-absolute-deviation) of a list of epochs in seconds."""
    sorted_epochs = sorted(epochs)
    n = len(sorted_epochs)
    if n % 2 == 1:
        median_epoch = sorted_epochs[n // 2]
    else:
        median_epoch = (sorted_epochs[n // 2 - 1] + sorted_epochs[n // 2]) / 2
    deviations = sorted(abs(e - median_epoch) for e in epochs)
    mad = deviations[n // 2] if n > 0 else 0.0
    return median_epoch, mad


def _median_int(values: list[int]) -> int:
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n % 2:
        return sorted_values[n // 2]
    return (sorted_values[n // 2 - 1] + sorted_values[n // 2]) // 2


def _datetime_to_epoch_ns(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.astimezone(UTC).timestamp() * 1_000_000_000)


def _epoch_ns_to_datetime(epoch_ns: int) -> datetime:
    seconds, nanoseconds = divmod(epoch_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=nanoseconds // 1000)


def _minute_phase_ns(rx_monotonic_ns: int, declared_monotonic_ns: int) -> int:
    return (rx_monotonic_ns - declared_monotonic_ns) % MINUTE_NS


def _observation_phase_ns(
    obs: "StationObservation",
    monotonic_now_ns: int,
    wall_now_epoch_ns: int,
) -> int | None:
    if obs.rx_monotonic_ns is None:
        return None
    declared_epoch_ns = _datetime_to_epoch_ns(obs.ct_utc)
    if obs.received_wall_utc is None:
        declared_monotonic_ns = monotonic_now_ns + declared_epoch_ns - wall_now_epoch_ns
    else:
        declared_monotonic_ns = (
            obs.rx_monotonic_ns + declared_epoch_ns - _datetime_to_epoch_ns(obs.received_wall_utc)
        )
    return _minute_phase_ns(obs.rx_monotonic_ns, declared_monotonic_ns)


def _apply_tx_latency(track: "StationTrack", tx_latency_ns: int) -> None:
    track.tx_latency_ns = tx_latency_ns
    for obs in track.observations:
        obs.tx_latency_ns = tx_latency_ns
        if obs.clock_time is not None:
            obs.clock_time = replace(obs.clock_time, tx_latency_ns=tx_latency_ns)


def _median_age_of_contributors(
    estimates: list[tuple["StationTrack", datetime]],
    epochs: list[float],
    median_epoch: float,
    monotonic_now: float,
) -> float:
    ages = [
        t.age_s(monotonic_now)
        for (t, _), epoch in zip(estimates, epochs, strict=True)
        if abs(epoch - median_epoch) <= OUTLIER_THRESHOLD_S
    ]
    return sorted(ages)[len(ages) // 2] if ages else float("inf")


def _consensus_notes(mad: float, n_contrib: int) -> str:
    if n_contrib > 1:
        return f"MAD={mad:.1f}s"
    if n_contrib == 1:
        return "single-source"
    return "outlier-only"


class TimeConsensus:
    """Aggregator that holds the station watchlist and computes the consensus."""

    def __init__(
        self,
        mission_precision_s: float = 60.0,
        local_osc_ppm: float = DEFAULT_LOCAL_OSC_PPM,
        stale_age_s: float = STALE_AGE_S,
    ):
        self.tracks: dict[tuple[float, int | None], StationTrack] = {}
        self.mission_precision_s = mission_precision_s
        self.local_osc_ppm = local_osc_ppm
        self.stale_age_s = stale_age_s

    def _key(self, freq_hz: float, pi: int | None) -> tuple[float, int | None]:
        # Round frequency to the nearest 100 kHz so minor tuner drift
        # doesn't split observations of the same station across keys.
        return (round(freq_hz / 100_000) * 100_000, pi)

    def record(self, obs: StationObservation) -> StationTrack:
        if obs.received_wall_utc is None:
            obs.received_wall_utc = datetime.now(UTC)
        key = self._key(obs.freq_hz, obs.pi)
        track = self.tracks.get(key)
        if track is None:
            track = StationTrack(freq_hz=obs.freq_hz, pi=obs.pi)
            self.tracks[key] = track
        track.add_observation(obs)
        if track.fingerprint_baseline is None:
            track.fingerprint_baseline = obs.fingerprint
        return track

    def sub_second_consensus(
        self,
        monotonic_now_ns: int | None = None,
        wall_now_utc: datetime | None = None,
    ) -> SubSecondEstimate | None:
        """Return a sub-second estimate when enough stations have RX timestamps.

        This path is intentionally parallel to :meth:`consensus`: it learns a
        per-station Group 4A transmit phase from timestamped observations and
        only contributes stations whose phase has at least three samples.
        """
        if monotonic_now_ns is None:
            monotonic_now_ns = time.monotonic_ns()
        wall_now_epoch_ns = (
            time.time_ns() if wall_now_utc is None else _datetime_to_epoch_ns(wall_now_utc)
        )

        self._update_station_latencies(monotonic_now_ns, wall_now_epoch_ns)
        station_estimates = self._sub_second_station_estimates(monotonic_now_ns, wall_now_epoch_ns)
        if len(station_estimates) < 2:
            return None

        median_epoch_ns = _median_int(station_estimates)
        deviations = [abs(epoch_ns - median_epoch_ns) for epoch_ns in station_estimates]
        precision_ms = float(_median_int(deviations) / 1_000_000.0)
        return SubSecondEstimate(
            utc=_epoch_ns_to_datetime(median_epoch_ns),
            precision_ms=precision_ms,
            station_count=len(station_estimates),
        )

    def _update_station_latencies(
        self,
        monotonic_now_ns: int,
        wall_now_epoch_ns: int,
    ) -> None:
        for track in self.tracks.values():
            phases: list[int] = []
            for obs in track.observations:
                phase = _observation_phase_ns(obs, monotonic_now_ns, wall_now_epoch_ns)
                if phase is not None:
                    phases.append(phase)
            if len(phases) >= 3:
                _apply_tx_latency(track, _median_int(phases))

    def _sub_second_station_estimates(
        self,
        monotonic_now_ns: int,
        wall_now_epoch_ns: int,
    ) -> list[int]:
        station_estimates: list[int] = []
        for track in self.tracks.values():
            if track.tx_latency_ns is None:
                continue
            obs = next(
                (
                    candidate
                    for candidate in reversed(track.observations)
                    if candidate.rx_monotonic_ns is not None
                ),
                None,
            )
            if obs is None:
                continue
            rx_wall_epoch_ns = wall_now_epoch_ns + (obs.rx_monotonic_ns - monotonic_now_ns)
            station_estimates.append(rx_wall_epoch_ns - track.tx_latency_ns)
        return station_estimates

    def active_tracks(self, monotonic_now: float) -> list[StationTrack]:
        return [
            t
            for t in self.tracks.values()
            if t.is_active(monotonic_now, max_age_s=self.stale_age_s)
        ]

    def consensus(self, monotonic_now: float | None = None) -> ConsensusResult:
        if monotonic_now is None:
            monotonic_now = time.monotonic()
        active = self.active_tracks(monotonic_now)
        if not active:
            return _empty_consensus_result()

        estimates = _collect_estimates(active, monotonic_now)
        if not estimates:
            return _empty_consensus_result()

        epochs = [(e - _UNIX_EPOCH).total_seconds() for _, e in estimates]
        median_epoch, mad = _median_and_mad(epochs)
        median_utc = _UNIX_EPOCH + timedelta(seconds=median_epoch)

        contributing, outliers = self._classify_tracks(estimates, epochs, median_epoch)

        median_age = _median_age_of_contributors(estimates, epochs, median_epoch, monotonic_now)

        uncertainty = max(mad / 2.0, median_age * self.local_osc_ppm * 1e-6, 1.0)
        trust = self._trust_level(len(contributing), median_age, uncertainty)

        return ConsensusResult(
            utc=median_utc,
            uncertainty_s=uncertainty,
            trust_level=trust,
            n_sources=len(contributing),
            contributing_freqs_mhz=sorted(contributing),
            outlier_freqs_mhz=sorted(outliers),
            median_age_s=median_age,
            notes=_consensus_notes(mad, len(contributing)),
        )

    def _classify_tracks(
        self,
        estimates: list[tuple[StationTrack, datetime]],
        epochs: list[float],
        median_epoch: float,
    ) -> tuple[list[float], list[float]]:
        contributing: list[float] = []
        outliers: list[float] = []
        for (t, _), epoch in zip(estimates, epochs, strict=True):
            if abs(epoch - median_epoch) > OUTLIER_THRESHOLD_S:
                t.consecutive_outliers += 1
                t.trust_score = max(0.0, t.trust_score - OUTLIER_PENALTY)
                outliers.append(t.freq_hz / 1e6)
            else:
                t.consecutive_outliers = 0
                t.trust_score = min(1.0, t.trust_score + AGREEMENT_BONUS)
                contributing.append(t.freq_hz / 1e6)
        return contributing, outliers

    def _trust_level(self, n_contrib: int, median_age: float, uncertainty: float) -> TrustLevel:
        if n_contrib >= 3 and median_age < 120 and uncertainty < 30:
            return TrustLevel.HIGH
        if n_contrib >= 2 and median_age < 180:
            return TrustLevel.MEDIUM
        if median_age < self.mission_precision_s:
            return TrustLevel.LOW
        return TrustLevel.STALE

    def summary(self, monotonic_now: float | None = None) -> str:
        """Tabular summary of every known station, sorted by trust score."""
        header = f"  {'STATION':<20} {'OBS':<5} {'TRUST':<7} {'OUTL':<5} {'AGE':<10} {'EST UTC'}"
        lines = [header]
        if monotonic_now is None:
            monotonic_now = time.monotonic()
        mono_now = monotonic_now
        for t in sorted(self.tracks.values(), key=lambda x: -x.trust_score):
            est = t.estimated_utc_now(mono_now)
            est_str = est.strftime("%H:%M:%S") if est else "—"
            age = t.age_s(mono_now)
            age_str = f"{age:.0f}s" if age < 600 else f"{age / 60:.1f}m"
            lines.append(
                f"  {t.label:<20} {len(t.observations):<5} "
                f"{t.trust_score:<7.2f} {t.consecutive_outliers:<5} "
                f"{age_str:<10} {est_str}"
            )
        return "\n".join(lines)
