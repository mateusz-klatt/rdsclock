"""What every ``rdsclock recon`` dashboard shows, without any UI code.

The terminal, web and desktop dashboards share this module. None of them
talks to the receiver: each starts ``rdsclock recon --jsonl`` as a child
process and feeds its stdout lines to :class:`DashboardState`, which
type-checks every record before applying it. :meth:`DashboardState.view`
returns display strings that a front end only lays out and styles;
:func:`rdsclock.recon.event_to_json` documents the records.

Every string that the state hands out comes from broadcasts or the child
process. It is reduced to printable characters, and front ends must render
it as plain text, never as markup.
"""

import contextlib
import json
import math
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import IO, Any

from .ct_policy import STANDARD_ZONE
from .i18n import normalize_language, translate
from .time_consensus import STALE_AGE_S

SUPPORTED_SCHEMA = 1
STDERR_TAIL_LINES = 20
LINE_LIMIT_BYTES = 1 << 20  # far above the largest cycle_completed record
UNCERTAINTY_NOTE = "± is source dispersion and holdover, not a calibrated UTC error."
COLUMNS = (
    "MHz",
    "PS",
    "PI",
    "RSSI dB",
    "Groups",
    "Corr.",
    "Last CT",
    "LTO",
    "Age",
    "Trust",
    "Status",
)
# Leads the columns when a run has several receivers.
RECEIVER_COLUMN = "Receiver"

STANDARD_CT = "standard"
# Host clock minus consensus this close to a whole, non-zero number of hours
# suggests stations that send local time in the UTC field.
WHOLE_HOUR_TOLERANCE_S = 120.0
WHOLE_HOUR_HINT = (
    "Host clock minus consensus is close to {hours:+d} h. Some stations may send local time "
    "in the CT UTC field; if so, name their time zone with --ct-timezone. The host clock is "
    "only a hint: no time is changed automatically."
)

Record = dict[str, Any]

_STOP_REASONS = {"iterations": "iteration limit reached", "interrupted": "stopped on request"}


def printable(value: object) -> str:
    """Keep broadcast and child-process text from controlling a terminal."""
    return "".join(ch if ch.isprintable() else "\N{REPLACEMENT CHARACTER}" for ch in str(value))


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"expected a number, got {type(value).__name__}")
    number = float(value)  # OverflowError for integers beyond the float range
    if not math.isfinite(number):
        raise ValueError("expected a finite number")
    return number


def _optional_number(value: object) -> float | None:
    return None if value is None else _number(value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    return printable(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected an integer, got {type(value).__name__}")
    return value


def _optional_integer(value: object) -> int | None:
    return None if value is None else _integer(value)


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    moment = datetime.fromisoformat(printable(value))
    if moment.tzinfo is None:
        raise ValueError("timestamp without a UTC offset")
    return moment


def _wide(record: Record) -> tuple[float, float] | None:
    """The WIDE capture centre and sample rate of an event, or ``None`` for HOP."""
    center_hz = _optional_number(record.get("wide_center_hz"))
    sample_rate = _optional_number(record.get("wide_sample_rate"))
    if center_hz is None or sample_rate is None:
        return None
    return center_hz, sample_rate


def _tuning(freq_hz: float) -> int:
    """A tuned frequency in whole hertz, so that equal tunings compare equal.

    Rows are keyed by the exact tuning rather than by a channel bin: scans
    with steps below 100 kHz can watch two tunings in the same bin.
    """
    return round(freq_hz)


RowKey = tuple[str | None, int]


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _row_key(receiver: str | None, freq_hz: float) -> RowKey:
    """A row: one tuning of one receiver.

    Two receivers may watch the same tuning, in different places perhaps
    with different transmitters. Streams without receivers use ``None``.
    """
    return receiver, _tuning(freq_hz)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _lto(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _pi(value: int | None) -> str:
    return "—" if value is None else f"0x{value:04X}"


def _db(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1f}"


@dataclass(frozen=True)
class Station:
    """A watchlist entry, as chosen by the acquisition scan.

    ``receiver`` is the ``host:port`` that records it; ``None`` in streams
    from versions that did not name receivers.
    """

    freq_hz: float
    ps_name: str
    pi: int | None
    rssi_db: float | None
    n_groups: int
    receiver: str | None = None

    @classmethod
    def from_record(cls, record: Record) -> "Station":
        return cls(
            freq_hz=_number(record["freq_hz"]),
            ps_name=printable(record["ps_name"]).strip(),
            pi=_optional_integer(record["pi"]),
            rssi_db=_optional_number(record["rssi_db"]),
            n_groups=_integer(record["n_groups"]),
            receiver=_optional_text(record.get("receiver")),
        )

    @property
    def key(self) -> RowKey:
        return _row_key(self.receiver, self.freq_hz)


@dataclass(frozen=True)
class Hop:
    """The latest ``hop_result`` of one channel.

    ``ct_utc`` and ``lto_minutes`` are the CT as broadcast. ``ct_status``,
    ``ct_timezone`` and ``effective_utc`` say how ``recon --ct-timezone`` read
    it; records without them come from the standard reading. ``receiver``
    is the ``host:port`` that recorded it, when the record names one.
    """

    ps_name: str
    pi: int | None
    rssi_db: float | None
    n_groups: int
    n_groups_corrected: int
    ct_utc: datetime | None
    lto_minutes: int | None
    ct_status: str = STANDARD_CT
    ct_timezone: str = STANDARD_ZONE
    effective_utc: datetime | None = None
    receiver: str | None = None

    @classmethod
    def from_record(cls, record: Record) -> "Hop":
        clock_time = record["clock_time"]
        ct_utc = None if clock_time is None else _timestamp(clock_time["utc"])
        status, zone, effective_utc = STANDARD_CT, STANDARD_ZONE, ct_utc
        if clock_time is not None and clock_time.get("ct_status", STANDARD_CT) != STANDARD_CT:
            status = _text(clock_time["ct_status"])
            zone = _text(clock_time["ct_timezone"])
            effective_utc = _timestamp(clock_time["effective_utc"])
            if status == "converted" and effective_utc is None:
                raise ValueError("a converted CT without effective_utc")
        return cls(
            ps_name=printable(record["ps_name"]).strip(),
            pi=_optional_integer(record["pi"]),
            rssi_db=_optional_number(record["rssi_db"]),
            n_groups=_integer(record["n_groups"]),
            n_groups_corrected=_integer(record["n_groups_corrected"]),
            ct_utc=ct_utc,
            lto_minutes=(
                None if clock_time is None else _integer(clock_time["local_offset_minutes"])
            ),
            ct_status=status,
            ct_timezone=zone,
            effective_utc=effective_utc,
            receiver=_optional_text(record.get("receiver")),
        )

    @property
    def read_as_local(self) -> bool:
        """Whether ``--ct-timezone`` read this CT as local time instead of UTC."""
        return self.ct_utc is not None and self.ct_status != STANDARD_CT


@dataclass(frozen=True)
class Track:
    """One consensus track: its PI, the age of its latest CT and its trust score."""

    pi: int | None
    age_s: float | None
    trust_score: float


def _not_older(track: Track, previous: Track) -> bool:
    if track.age_s is None:
        return previous.age_s is None
    return previous.age_s is None or track.age_s <= previous.age_s


@dataclass(frozen=True)
class Consensus:
    """The consensus result of one cycle."""

    utc: datetime | None
    uncertainty_s: float | None
    trust_level: str
    n_sources: int
    contributing: frozenset[int]
    outliers: frozenset[int]
    outliers_mhz: tuple[float, ...]
    median_age_s: float | None
    notes: str

    @classmethod
    def from_record(cls, record: Record) -> "Consensus":
        outliers_mhz = tuple(_number(freq) for freq in record["outlier_freqs_mhz"])
        return cls(
            utc=_timestamp(record["utc"]),
            uncertainty_s=_optional_number(record["uncertainty_s"]),
            trust_level=printable(record["trust_level"]),
            n_sources=_integer(record["n_sources"]),
            contributing=frozenset(
                _tuning(_number(freq) * 1e6) for freq in record["contributing_freqs_mhz"]
            ),
            outliers=frozenset(_tuning(freq * 1e6) for freq in outliers_mhz),
            outliers_mhz=outliers_mhz,
            median_age_s=_optional_number(record["median_age_s"]),
            notes=printable(record["notes"]),
        )


@dataclass(frozen=True)
class ConsensusView:
    """The consensus, advanced by the dashboard clock, as display strings.

    ``utc`` is ``None`` while no time is available, including after the
    estimate has expired. ``trust_level`` stays a plain level name so that
    every front end can choose its own styling; ``assessed`` says when the
    receiver last assessed it.
    """

    trust_level: str
    utc: str | None
    uncertainty: str
    sources: str
    age: str | None
    host_offset: str | None
    outliers: str | None
    notes: str
    assessed: str


@dataclass(frozen=True)
class Row:
    """One station: the cells under the columns but the last, and the status text.

    The columns are :meth:`DashboardState.columns`: ``COLUMNS``, led by
    ``RECEIVER_COLUMN`` when the run has several receivers.
    """

    cells: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class View:
    """A complete snapshot for a front end to lay out."""

    phase: str
    status: str
    consensus: ConsensusView | None
    rows: tuple[Row, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DashboardState:
    """Everything a dashboard shows, built from recon events.

    ``clock`` is the dashboard's own monotonic clock. Between two
    ``cycle_completed`` events it advances the consensus time, the ages and
    the rescan countdown. ``wall_clock`` reads the host clock for the
    difference to the consensus, so that a host clock change shows at once.
    """

    clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], datetime] = _utc_now
    phase: str = "STARTING"
    iteration: int = 0
    ready: bool = False
    stopped: bool = False
    watchlist: list[Station] = field(default_factory=list)
    # Rows are keyed by receiver and tuning (see _row_key).
    hops: dict[RowKey, Hop] = field(default_factory=dict)
    last_ct: dict[RowKey, Hop] = field(default_factory=dict)
    identity: dict[RowKey, int] = field(default_factory=dict)  # last confirmed PI per row
    consensus: Consensus | None = None
    tracks: dict[int, Track] = field(default_factory=dict)
    next_rescan_in_s: float | None = None
    cycle_at: float = 0.0
    band: tuple[float, float, float] | None = None
    scan_total: int | None = None  # stations of a listed scan; None for a band
    wide: tuple[float, float] | None = None  # WIDE capture centre and rate; None for HOP
    scan_points: int = 0
    scan_freq_hz: float | None = None
    hops_done: int = 0
    n_stations: int = 0
    schema_warned: bool = False
    language: str = "en"
    ct_rules: tuple[str, ...] = ()  # the receiver's --ct-timezone rules
    ct_read_as_local: bool = False  # some CT was read as local time
    hinted_hours: int | None = None  # whole-hour host offset last hinted at
    receivers: tuple[str, ...] = ()  # every receiver of the run, from recon_started
    failed_receivers: dict[str, str] = field(default_factory=dict)  # latest error of each
    pass_hops: dict[str | None, int] = field(default_factory=dict)  # this pass, per receiver

    @property
    def multi_receiver(self) -> bool:
        """Whether the run uses several receivers, so that rows should name theirs."""
        named = {station.receiver for station in self.watchlist} - {None}
        return len(set(self.receivers) | named) > 1

    def columns(self) -> tuple[str, ...]:
        """The column names of :attr:`View.rows`, the status last, untranslated."""
        return (RECEIVER_COLUMN, *COLUMNS) if self.multi_receiver else COLUMNS

    def _receiver_order(self) -> list[str]:
        """Every receiver: those of ``recon_started``, then any other the watchlist names."""
        named = [station.receiver for station in self.watchlist if station.receiver is not None]
        return list(dict.fromkeys([*self.receivers, *named]))

    def _receiver_back(self, receivers: Iterable[str | None]) -> list[str]:
        """Messages for failed receivers that an event shows working again."""
        messages = []
        for receiver in dict.fromkeys(receivers):
            if receiver is not None and self.failed_receivers.pop(receiver, None) is not None:
                messages.append(self._t("Receiver {receiver} is working again.", receiver=receiver))
        return messages

    def __post_init__(self) -> None:
        self.language = normalize_language(self.language)

    def _t(self, message: str, **values: object) -> str:
        return translate(self.language, message, **values)

    def apply_line(self, line: str) -> list[str]:
        """Apply one JSON Lines record; return messages for the operator log."""
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return [self._t("Ignored a line that is not JSON.")]
        if not isinstance(record, dict):
            return [self._t("Ignored a JSON value that is not an event.")]
        messages = []
        if record.get("schema") != SUPPORTED_SCHEMA and not self.schema_warned:
            self.schema_warned = True
            messages.append(
                self._t(
                    "Event schema {schema} is not {supported}; "
                    "showing what this version understands.",
                    schema=record.get("schema"),
                    supported=SUPPORTED_SCHEMA,
                )
            )
        kind = record.get("event")
        handler = _HANDLERS.get(kind) if isinstance(kind, str) else None
        if handler is not None:
            try:
                messages.extend(handler(self, record))
            except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError):
                messages.append(self._t("Ignored a malformed {kind} event.", kind=kind))
        return [printable(message) for message in messages]

    # Each handler validates the whole record before it changes any state.

    def _on_recon_started(self, record: Record) -> list[str]:
        tuner, gains = _integer(record["tuner_type"]), _integer(record["gain_count"])
        listed = record.get("ct_timezone_rules", [])
        if not isinstance(listed, list):
            raise ValueError("ct_timezone_rules is not a list")
        rules = tuple(_text(rule) for rule in listed)
        named = record.get("receivers", [])
        if not isinstance(named, list):
            raise ValueError("receivers is not a list")
        receivers = tuple(_text(receiver) for receiver in named)
        self.ready, self.ct_rules, self.receivers = True, rules, receivers
        messages = [
            self._t(
                "Receiver ready: tuner type {tuner}, {gains} gain steps.", tuner=tuner, gains=gains
            )
        ]
        if rules:
            messages.append(
                self._t("CT read as local time by --ct-timezone: {rules}.", rules=", ".join(rules))
            )
        return messages

    def _on_acquisition_started(self, record: Record) -> list[str]:
        iteration = _integer(record["iteration"])
        band = tuple(
            _number(record[key]) for key in ("band_start_mhz", "band_end_mhz", "scan_step_mhz")
        )
        wide = _wide(record)
        listed = _optional_integer(record.get("n_points"))
        self.phase, self.iteration, self.band, self.wide = "ACQUISITION", iteration, band, wide
        self.scan_points, self.scan_freq_hz, self.scan_total = 0, None, listed
        if wide is None and listed is not None:
            return [
                self._t(
                    "Acquisition #{iteration}: checking {listed} listed stations, "
                    "{start:.2f}-{end:.2f} MHz.",
                    iteration=iteration,
                    listed=listed,
                    start=band[0],
                    end=band[1],
                )
            ]
        if wide is not None:
            return [
                self._t(
                    "Acquisition #{iteration}: one WIDE capture at {center:.2f} MHz "
                    "covers {start:.2f}-{end:.2f} MHz.",
                    iteration=iteration,
                    center=wide[0] / 1e6,
                    start=band[0],
                    end=band[1],
                )
            ]
        return [
            self._t(
                "Acquisition #{iteration}: scanning {start:.2f}-{end:.2f} MHz "
                "in {step:.2f} MHz steps.",
                iteration=iteration,
                start=band[0],
                end=band[1],
                step=band[2],
            )
        ]

    def _on_scan_step(self, record: Record) -> list[str]:
        freq_hz = _number(record["freq_hz"])
        candidate = record["candidate"]
        station = None if candidate is None else Station.from_record(candidate)
        self.scan_points += 1
        self.scan_freq_hz = freq_hz
        back = self._receiver_back([None if station is None else station.receiver])
        if station is None or not station.n_groups:
            return back
        ct = ", CT" if candidate.get("has_ct") is True else ""
        return [
            *back,
            self._t(
                "  {freq:6.2f} MHz  {db} dB  {groups} groups{ct}  {ps}",
                freq=freq_hz / 1e6,
                db=_db(station.rssi_db),
                groups=station.n_groups,
                ct=ct,
                ps=station.ps_name,
            ).rstrip(),
        ]

    def _on_watchlist_updated(self, record: Record) -> list[str]:
        watchlist = [Station.from_record(entry) for entry in record["watchlist"]]
        watched = {station.key for station in watchlist}
        self.watchlist = watchlist
        for cache in (self.hops, self.last_ct, self.identity):
            for key in set(cache) - watched:
                del cache[key]
        for station in watchlist:
            self._confirm(station.key, station.pi)
        back = self._receiver_back(station.receiver for station in watchlist)
        if not watchlist:
            return [*back, self._t("Watchlist empty: no RDS station found in this scan.")]
        if self.multi_receiver:
            # As --hop names them: HOST:PORT@MHZ.
            freqs = ", ".join(f"{s.receiver}@{s.freq_hz / 1e6:.2f}" for s in watchlist)
        else:
            freqs = ", ".join(f"{station.freq_hz / 1e6:.2f}" for station in watchlist)
        return [*back, self._t("Watchlist: {freqs} MHz.", freqs=freqs)]

    def _on_maintenance_started(self, record: Record) -> list[str]:
        iteration, n_stations = _integer(record["iteration"]), _integer(record["n_stations"])
        wide = _wide(record)
        self.phase, self.wide = "MAINTENANCE", wide
        self.iteration, self.n_stations, self.hops_done = iteration, n_stations, 0
        self.pass_hops = {}
        if wide is None or not n_stations:
            return []
        return [
            self._t(
                "WIDE capture at {center:.2f} MHz, {rate:.2f} MS/s: "
                "{n_stations} stations at once.",
                center=wide[0] / 1e6,
                rate=wide[1] / 1e6,
                n_stations=n_stations,
            )
        ]

    def _on_hop_result(self, record: Record) -> list[str]:
        freq_hz = _number(record["freq_hz"])
        hop = Hop.from_record(record)
        key = _row_key(hop.receiver, freq_hz)
        back = self._receiver_back([hop.receiver])
        self._confirm(key, hop.pi)
        self.hops[key] = hop
        # CT is missing from many captures: retain the last confirmed source.
        # An unconfirmed PI cannot replace it and later masquerade as that PI.
        # Its CT remains visible in the current hop, without entering the cache.
        if hop.ct_utc is not None and hop.pi is not None:
            self.last_ct[key] = hop
        self.hops_done += 1
        self.pass_hops[hop.receiver] = self.pass_hops.get(hop.receiver, 0) + 1
        return [*back, *self._hop_messages(freq_hz, hop)]

    def _hop_messages(self, freq_hz: float, hop: Hop) -> list[str]:
        label = f"{freq_hz / 1e6:.2f} MHz {hop.ps_name}".rstrip()
        if self.multi_receiver and hop.receiver is not None:
            label = f"[{hop.receiver}] {label}"
        groups = self._t(
            "{n_groups} groups, {corrected} corrected",
            n_groups=hop.n_groups,
            corrected=hop.n_groups_corrected,
        )
        if hop.ct_utc is None:
            return [self._t("{label}: no CT, {groups}.", label=label, groups=groups)]
        lto = _lto(hop.lto_minutes)
        if not hop.read_as_local:
            return [
                self._t(
                    "{label}: CT {utc:%H:%M}Z (LTO {lto}), {groups}.",
                    label=label,
                    utc=hop.ct_utc,
                    lto=lto,
                    groups=groups,
                )
            ]
        self.ct_read_as_local = True
        values = {"label": label, "local": hop.ct_utc, "zone": hop.ct_timezone, "lto": lto}
        if hop.effective_utc is not None:
            return [
                self._t(
                    "{label}: CT {local:%H:%M} read as {zone} local time = {utc:%H:%M}Z "
                    "(LTO {lto}), {groups}.",
                    utc=hop.effective_utc,
                    groups=groups,
                    **values,
                )
            ]
        return [
            self._t(
                "{label}: CT {local:%H:%M} as {zone} local time is {status}, not used "
                "(LTO {lto}), {groups}.",
                status=self._t(hop.ct_status),
                groups=groups,
                **values,
            )
        ]

    def _on_cycle_completed(self, record: Record) -> list[str]:
        iteration = _integer(record["iteration"])
        new_observations = _integer(record["new_observations"])
        consensus = Consensus.from_record(record["consensus"])
        watchlist = [Station.from_record(entry) for entry in record["watchlist"]]
        # A PI change leaves several tracks per tuning; like the consensus,
        # keep the freshest one (ties go to the later track).
        tracks: dict[int, Track] = {}
        for entry in record["tracks"]:
            key = _tuning(_number(entry["freq_hz"]))
            track = Track(
                pi=_optional_integer(entry["pi"]),
                age_s=_optional_number(entry["age_s"]),
                trust_score=_number(entry["trust_score"]),
            )
            if key not in tracks or _not_older(track, tracks[key]):
                tracks[key] = track
        next_rescan_in_s = _number(record["next_rescan_in_s"])
        self.consensus, self.tracks, self.watchlist = consensus, tracks, watchlist
        self.next_rescan_in_s = next_rescan_in_s
        self.cycle_at = self.clock()
        return [
            self._t(
                "Cycle #{iteration}: {observations} new CT observation(s); "
                "trust {trust}, N={sources}.",
                iteration=iteration,
                observations=new_observations,
                trust=self._t(consensus.trust_level),
                sources=consensus.n_sources,
            ),
            *self._whole_hour_hint(consensus),
        ]

    def _whole_hour_hint(self, consensus: Consensus) -> list[str]:
        """Suggest ``--ct-timezone`` once each time a whole-hour host offset appears or changes.

        Only without any rule: the host clock is a hint, never a reference, so
        nothing else follows from it.
        """
        if consensus.utc is None:
            return []
        hours = None
        if not (self.ct_rules or self.ct_read_as_local):
            delta_s = (self.wall_clock() - consensus.utc).total_seconds()
            nearest = round(delta_s / 3600)
            if nearest and abs(delta_s - nearest * 3600) <= WHOLE_HOUR_TOLERANCE_S:
                hours = nearest
        previous, self.hinted_hours = self.hinted_hours, hours
        if hours is None or hours == previous:
            return []
        return [self._t(WHOLE_HOUR_HINT, hours=hours)]

    def _on_receiver_failed(self, record: Record) -> list[str]:
        receiver, error = _text(record["receiver"]), _text(record["error"])
        _integer(record["iteration"])
        self.failed_receivers[receiver] = error
        # recon reports no reconnection: the next event naming the receiver shows it.
        return [
            self._t(
                "Receiver {receiver} failed: {error}; reconnecting next cycle.",
                receiver=receiver,
                error=error,
            )
        ]

    def _on_recon_stopped(self, record: Record) -> list[str]:
        reason = printable(record["reason"])
        self.stopped, self.phase = True, "STOPPED"
        return [
            self._t(
                "Receiver stopped: {reason}.", reason=self._t(_STOP_REASONS.get(reason, reason))
            )
        ]

    def receiver_exited(
        self, returncode: int, stderr_tail: Iterable[str], requested: bool
    ) -> list[str]:
        """Messages for a child process that has exited; quiet after a clean stop."""
        self.phase = "STOPPED"
        if returncode == 0 and (self.stopped or requested):
            return []
        if returncode == 0:
            messages = [self._t("The receiver stream ended without recon_stopped.")]
        else:
            messages = [
                self._t("Receiver process exited with status {returncode}.", returncode=returncode)
            ]
        messages.extend(f"  {line}" for line in stderr_tail)
        return [printable(message) for message in messages]

    def exit_failed(self, returncode: int, requested: bool) -> bool:
        """Whether the child ended on its own without a clean ``recon_stopped``.

        A stream that simply ends was cut short, even with exit status 0.
        """
        return not requested and (returncode != 0 or not self.stopped)

    def _confirm(self, key: RowKey, pi: int | None) -> None:
        """Record a confirmed PI; a different one invalidates the tuning's caches."""
        if pi is None:
            return
        if self.identity.get(key, pi) != pi:
            self.hops.pop(key, None)
            self.last_ct.pop(key, None)
        self.identity[key] = pi

    def start_failed(self, error: OSError) -> list[str]:
        self.phase = "STOPPED"
        return [printable(self._t("Could not start the receiver: {error}", error=error))]

    def elapsed(self) -> float:
        """Seconds since the latest ``cycle_completed`` event arrived."""
        return 0.0 if self.consensus is None else self.clock() - self.cycle_at

    def can_rescan(self) -> bool:
        """The child handles SIGUSR1 once it has reported ``recon_started``."""
        return self.ready and not self.stopped and self.phase != "STOPPED"

    def view(self) -> View:
        """The current snapshot, advanced by the dashboard clock."""
        return View(
            phase=self.phase,
            status=self._status_line(),
            consensus=self._consensus_view(),
            rows=tuple(self._row(station) for station in self.watchlist),
        )

    def _status_line(self) -> str:
        status = self._t(self.phase)
        if self.iteration:
            status += f" #{self.iteration}"
        if self.phase == "ACQUISITION" and self.wide is not None:
            status += f" · WIDE {self.wide[0] / 1e6:.2f} MHz"
        elif self.phase == "ACQUISITION" and self.band is not None:
            start, end, step = self.band
            total = math.floor((end - start) / step + 1e-6) + 1 if step > 0 else 0
            progress = " · {completed}/{total} points"
            if self.scan_total is not None:
                total, progress = self.scan_total, " · {completed}/{total} stations"
            if self.scan_freq_hz is not None:
                status += f" · {self.scan_freq_hz / 1e6:.2f} MHz"
            status += self._t(progress, completed=self.scan_points, total=total)
        elif self.phase == "MAINTENANCE":
            status += self._hop_progress() if self.wide is None else self._wide_progress()
        if self.next_rescan_in_s is not None and self.phase != "STOPPED":
            remaining = max(0.0, self.next_rescan_in_s - self.elapsed())
            status += self._t(" · next rescan in {remaining:.0f} s", remaining=remaining)
        return status

    def _hop_progress(self) -> str:
        if not self.n_stations:
            return self._t(" · no stations to visit")
        if self.multi_receiver:
            return self._parallel_hop_progress()
        progress = self._t(
            " · hop {current}/{total}",
            current=min(self.hops_done + 1, self.n_stations),
            total=self.n_stations,
        )
        if self.hops_done < min(self.n_stations, len(self.watchlist)):
            progress += self._t(
                " · recording {freq:.2f} MHz", freq=self.watchlist[self.hops_done].freq_hz / 1e6
            )
        return progress

    def _parallel_hop_progress(self) -> str:
        """What every receiver records now: each hops through its own stations at once."""
        progress = self._t(
            " · HOP · {done}/{total} stations recorded",
            done=min(self.hops_done, self.n_stations),
            total=self.n_stations,
        )
        for receiver in self._receiver_order():
            if receiver in self.failed_receivers:
                progress += self._t(" · {receiver}: failed", receiver=receiver)
                continue
            stations = [s for s in self.watchlist if s.receiver == receiver]
            done = self.pass_hops.get(receiver, 0)
            if done < len(stations):
                progress += self._t(
                    " · {receiver} → {freq:.2f} MHz",
                    receiver=receiver,
                    freq=stations[done].freq_hz / 1e6,
                )
            elif stations:
                progress += self._t(" · {receiver}: done", receiver=receiver)
        return progress

    def _wide_progress(self) -> str:
        if not self.n_stations:
            return self._t(" · no stations to record")
        progress = f" · WIDE {self.wide[0] / 1e6:.2f} MHz"
        if self.hops_done < self.n_stations:
            return progress + self._t(
                " · recording {stations} stations at once", stations=self.n_stations
            )
        return progress + self._t(
            " · {stations} stations from one capture", stations=self.n_stations
        )

    def _consensus_view(self) -> ConsensusView | None:
        consensus = self.consensus
        if consensus is None:
            return None
        elapsed = self.elapsed()
        assessed = self._t("assessed {elapsed:.0f} s ago", elapsed=elapsed)
        sources = f"N={consensus.n_sources}"
        age_s = None if consensus.median_age_s is None else consensus.median_age_s + elapsed
        if consensus.utc is not None and age_s is not None and age_s > STALE_AGE_S:
            # The consensus would no longer count these observations as active.
            return ConsensusView(
                trust_level="STALE",
                utc=None,
                uncertainty="±? s",
                sources=sources,
                age=None,
                host_offset=None,
                outliers=None,
                notes=self._t("the last estimate expired: its CT is {age:.0f} s old", age=age_s),
                assessed=assessed,
            )
        outliers = None
        if consensus.outliers_mhz:
            outliers = self._t(
                "Outliers: {freqs} MHz", freqs=", ".join(f"{f:.2f}" for f in consensus.outliers_mhz)
            )
        uncertainty = "?" if consensus.uncertainty_s is None else f"{consensus.uncertainty_s:.0f}"
        utc = age = host_offset = None
        if consensus.utc is not None:
            now = consensus.utc + timedelta(seconds=elapsed)
            utc = f"{now:%Y-%m-%d %H:%M:%S}"
            if age_s is not None:
                age = self._t("age {age:.0f} s", age=age_s)
            delta = (self.wall_clock() - now).total_seconds()
            host_offset = self._t("Host clock minus consensus: {delta:+.1f} s", delta=delta)
        return ConsensusView(
            trust_level=consensus.trust_level,
            utc=utc,
            uncertainty=f"±{uncertainty} s",
            sources=sources,
            age=age,
            host_offset=host_offset,
            outliers=outliers,
            notes=self._t(consensus.notes),
            assessed=assessed,
        )

    def _row(self, station: Station) -> Row:
        key, tuning = station.key, _tuning(station.freq_hz)
        hop = self.hops.get(key)
        last_ct = self.last_ct.get(key)
        if hop is not None and hop.pi is None and hop.ct_utc is not None:
            last_ct = hop
        # Consensus tracks follow the transmitter, not the receiver: by tuning.
        track = self.tracks.get(tuning)
        identity = self.identity.get(key)
        if track is not None and None not in (track.pi, identity) and track.pi != identity:
            track = None  # its metrics belong to the transmitter that answered before
        if hop is None:
            ps, pi, rssi, groups = station.ps_name, station.pi, station.rssi_db, station.n_groups
            corrected = ""
        else:
            # An unconfirmed hop PI stays unknown, and the scan's name is reused
            # only for the same confirmed PI: both may belong to another
            # transmitter.
            same = hop.pi is not None and hop.pi == station.pi
            ps, pi = hop.ps_name or (station.ps_name if same else ""), hop.pi
            rssi, groups, corrected = hop.rssi_db, hop.n_groups, str(hop.n_groups_corrected)
        ct = lto = ""
        if last_ct is not None:
            ct, lto = self._ct_cell(last_ct), _lto(last_ct.lto_minutes)
        elif hop is not None:
            ct = self._t("none")
        age = trust = "—"
        if track is not None:
            trust = f"{track.trust_score:.2f}"
            if track.age_s is not None:
                age = f"{track.age_s + self.elapsed():.0f} s"
        cells = (f"{station.freq_hz / 1e6:.2f}", ps, _pi(pi), _db(rssi), str(groups), corrected)
        cells += (ct, lto, age, trust)
        if self.multi_receiver:
            cells = (station.receiver or "", *cells)
        attributed = track is not None or tuning not in self.tracks
        return Row(cells=cells, status=self._station_status(key, hop, track, attributed))

    def _ct_cell(self, hop: Hop) -> str:
        """The CT as the consensus saw it: UTC, marked when read as local time."""
        if not hop.read_as_local:
            return f"{hop.ct_utc:%H:%M}Z"
        if hop.effective_utc is not None:
            return self._t("{utc:%H:%M}Z ← {zone}", utc=hop.effective_utc, zone=hop.ct_timezone)
        return self._t(
            "{local:%H:%M} {zone}: {status}, not used",
            local=hop.ct_utc,
            zone=hop.ct_timezone,
            status=self._t(hop.ct_status),
        )

    def _station_status(
        self, key: RowKey, hop: Hop | None, track: Track | None, attributed: bool
    ) -> str:
        tuning = key[1]
        if attributed and self.consensus is not None and tuning in self.consensus.outliers:
            return "OUTLIER"
        if attributed and self.consensus is not None and tuning in self.consensus.contributing:
            return "ok"
        if track is not None:
            return "not counted"
        if hop is not None and hop.ct_utc is None and key not in self.last_ct:
            return "no CT"
        return ""


_HANDLERS: dict[str, Callable[[DashboardState, Record], list[str]]] = {
    "recon_started": DashboardState._on_recon_started,
    "acquisition_started": DashboardState._on_acquisition_started,
    "scan_step": DashboardState._on_scan_step,
    "watchlist_updated": DashboardState._on_watchlist_updated,
    "maintenance_started": DashboardState._on_maintenance_started,
    "hop_result": DashboardState._on_hop_result,
    "cycle_completed": DashboardState._on_cycle_completed,
    "recon_stopped": DashboardState._on_recon_stopped,
    "receiver_failed": DashboardState._on_receiver_failed,
}


def bounded_lines(stream: IO[bytes], limit: int = LINE_LIMIT_BYTES) -> Iterator[str]:
    """Decoded lines of ``stream``; a line longer than ``limit`` becomes ``""``.

    The empty line is reported as not JSON, and memory stays bounded.
    """
    while chunk := stream.readline(limit + 1):
        if len(chunk) > limit and not chunk.endswith(b"\n"):
            while chunk and not chunk.endswith(b"\n"):  # drop the rest of the line
                chunk = stream.readline(limit + 1)
            yield ""
            continue
        yield chunk.decode("utf-8", errors="replace")


class ReceiverProcess:
    """``rdsclock recon --jsonl`` as a child process, read by background threads.

    For front ends without an event loop of their own. ``on_line`` receives
    every stdout line and ``on_exit`` the exit status and the stderr tail, both
    on a reader thread.
    """

    def __init__(
        self,
        command: list[str],
        on_line: Callable[[str], None],
        on_exit: Callable[[int, list[str]], None],
    ) -> None:
        self.command = command
        self.on_line = on_line
        self.on_exit = on_exit
        self.process: subprocess.Popen[bytes] | None = None
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        """Start the child and its readers; raises ``OSError`` if it cannot start."""
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stderr = threading.Thread(target=self._keep_stderr_tail, daemon=True)
        stdout = threading.Thread(target=self._follow, args=(stderr,), daemon=True)
        self._threads = [stderr, stdout]
        for thread in self._threads:
            thread.start()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _follow(self, stderr: threading.Thread) -> None:
        with self.process.stdout:
            for line in bounded_lines(self.process.stdout):
                self.on_line(line)
        returncode = self.process.wait()
        stderr.join()  # the tail is complete once stderr reaches end of file
        self.on_exit(returncode, list(self.stderr_tail))

    def _keep_stderr_tail(self) -> None:
        # Draining stderr also keeps the child from blocking on a full pipe.
        with self.process.stderr:
            for line in bounded_lines(self.process.stderr):
                self.stderr_tail.append(line.rstrip())

    def send_signal(self, signum: int) -> bool:
        """Send ``signum`` while the child runs; report whether it was sent."""
        if not self.running:
            return False
        with contextlib.suppress(ProcessLookupError):
            self.process.send_signal(signum)
            return True
        return False

    def stop(self, timeout_s: float) -> bool:
        """Interrupt the child as Ctrl+C would; kill it after ``timeout_s``.

        Returns ``False`` when the child had to be killed.
        """
        if not self.send_signal(signal.SIGINT):
            return True
        try:
            self.process.wait(timeout_s)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            return False
        return True

    def join(self, timeout_s: float | None = None) -> None:
        """Wait for the readers, and so for ``on_exit``, to finish."""
        for thread in self._threads:
            thread.join(timeout_s)
