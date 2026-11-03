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
    """A watchlist entry, as chosen by the acquisition scan."""

    freq_hz: float
    ps_name: str
    pi: int | None
    rssi_db: float | None
    n_groups: int

    @classmethod
    def from_record(cls, record: Record) -> "Station":
        return cls(
            freq_hz=_number(record["freq_hz"]),
            ps_name=printable(record["ps_name"]).strip(),
            pi=_optional_integer(record["pi"]),
            rssi_db=_optional_number(record["rssi_db"]),
            n_groups=_integer(record["n_groups"]),
        )


@dataclass(frozen=True)
class Hop:
    """The latest ``hop_result`` of one channel."""

    ps_name: str
    pi: int | None
    rssi_db: float | None
    n_groups: int
    n_groups_corrected: int
    ct_utc: datetime | None
    lto_minutes: int | None

    @classmethod
    def from_record(cls, record: Record) -> "Hop":
        clock_time = record["clock_time"]
        return cls(
            ps_name=printable(record["ps_name"]).strip(),
            pi=_optional_integer(record["pi"]),
            rssi_db=_optional_number(record["rssi_db"]),
            n_groups=_integer(record["n_groups"]),
            n_groups_corrected=_integer(record["n_groups_corrected"]),
            ct_utc=None if clock_time is None else _timestamp(clock_time["utc"]),
            lto_minutes=(
                None if clock_time is None else _integer(clock_time["local_offset_minutes"])
            ),
        )


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
    """One station: the cells under ``COLUMNS[:-1]`` and the status text."""

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
    hops: dict[int, Hop] = field(default_factory=dict)
    last_ct: dict[int, Hop] = field(default_factory=dict)
    identity: dict[int, int] = field(default_factory=dict)  # last confirmed PI per tuning
    consensus: Consensus | None = None
    tracks: dict[int, Track] = field(default_factory=dict)
    next_rescan_in_s: float | None = None
    cycle_at: float = 0.0
    band: tuple[float, float, float] | None = None
    wide: tuple[float, float] | None = None  # WIDE capture centre and rate; None for HOP
    scan_points: int = 0
    scan_freq_hz: float | None = None
    hops_done: int = 0
    n_stations: int = 0
    schema_warned: bool = False

    def apply_line(self, line: str) -> list[str]:
        """Apply one JSON Lines record; return messages for the operator log."""
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return ["Ignored a line that is not JSON."]
        if not isinstance(record, dict):
            return ["Ignored a JSON value that is not an event."]
        messages = []
        if record.get("schema") != SUPPORTED_SCHEMA and not self.schema_warned:
            self.schema_warned = True
            messages.append(
                f"Event schema {record.get('schema')} is not {SUPPORTED_SCHEMA}; "
                "showing what this version understands."
            )
        kind = record.get("event")
        handler = _HANDLERS.get(kind) if isinstance(kind, str) else None
        if handler is not None:
            try:
                messages.extend(handler(self, record))
            except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError):
                messages.append(f"Ignored a malformed {kind} event.")
        return [printable(message) for message in messages]

    # Each handler validates the whole record before it changes any state.

    def _on_recon_started(self, record: Record) -> list[str]:
        tuner, gains = _integer(record["tuner_type"]), _integer(record["gain_count"])
        self.ready = True
        return [f"Receiver ready: tuner type {tuner}, {gains} gain steps."]

    def _on_acquisition_started(self, record: Record) -> list[str]:
        iteration = _integer(record["iteration"])
        band = tuple(
            _number(record[key]) for key in ("band_start_mhz", "band_end_mhz", "scan_step_mhz")
        )
        wide = _wide(record)
        self.phase, self.iteration, self.band, self.wide = "ACQUISITION", iteration, band, wide
        self.scan_points, self.scan_freq_hz = 0, None
        if wide is not None:
            return [
                f"Acquisition #{iteration}: one WIDE capture at {wide[0] / 1e6:.2f} MHz "
                f"covers {band[0]:.2f}-{band[1]:.2f} MHz."
            ]
        return [
            f"Acquisition #{iteration}: scanning {band[0]:.2f}-{band[1]:.2f} MHz "
            f"in {band[2]:.2f} MHz steps."
        ]

    def _on_scan_step(self, record: Record) -> list[str]:
        freq_hz = _number(record["freq_hz"])
        candidate = record["candidate"]
        station = None if candidate is None else Station.from_record(candidate)
        self.scan_points += 1
        self.scan_freq_hz = freq_hz
        if station is None or not station.n_groups:
            return []
        ct = ", CT" if candidate.get("has_ct") is True else ""
        return [
            f"  {freq_hz / 1e6:6.2f} MHz  {_db(station.rssi_db)} dB  "
            f"{station.n_groups} groups{ct}  {station.ps_name}".rstrip()
        ]

    def _on_watchlist_updated(self, record: Record) -> list[str]:
        watchlist = [Station.from_record(entry) for entry in record["watchlist"]]
        watched = {_tuning(station.freq_hz) for station in watchlist}
        self.watchlist = watchlist
        for cache in (self.hops, self.last_ct, self.identity):
            for key in set(cache) - watched:
                del cache[key]
        for station in watchlist:
            self._confirm(_tuning(station.freq_hz), station.pi)
        if not watchlist:
            return ["Watchlist empty: no RDS station found in this scan."]
        freqs = ", ".join(f"{station.freq_hz / 1e6:.2f}" for station in watchlist)
        return [f"Watchlist: {freqs} MHz."]

    def _on_maintenance_started(self, record: Record) -> list[str]:
        iteration, n_stations = _integer(record["iteration"]), _integer(record["n_stations"])
        wide = _wide(record)
        self.phase, self.wide = "MAINTENANCE", wide
        self.iteration, self.n_stations, self.hops_done = iteration, n_stations, 0
        if wide is None or not n_stations:
            return []
        return [
            f"WIDE capture at {wide[0] / 1e6:.2f} MHz, {wide[1] / 1e6:.2f} MS/s: "
            f"{n_stations} stations at once."
        ]

    def _on_hop_result(self, record: Record) -> list[str]:
        freq_hz = _number(record["freq_hz"])
        hop = Hop.from_record(record)
        key = _tuning(freq_hz)
        self._confirm(key, hop.pi)
        self.hops[key] = hop
        # CT is missing from many captures: keep the latest one until another
        # transmitter is confirmed on this tuning.
        if hop.ct_utc is not None:
            self.last_ct[key] = hop
        self.hops_done += 1
        label = f"{freq_hz / 1e6:.2f} MHz {hop.ps_name}".rstrip()
        groups = f"{hop.n_groups} groups, {hop.n_groups_corrected} corrected"
        if hop.ct_utc is None:
            return [f"{label}: no CT, {groups}."]
        return [f"{label}: CT {hop.ct_utc:%H:%M}Z (LTO {_lto(hop.lto_minutes)}), {groups}."]

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
            f"Cycle #{iteration}: {new_observations} new CT observation(s); "
            f"trust {consensus.trust_level}, N={consensus.n_sources}."
        ]

    def _on_recon_stopped(self, record: Record) -> list[str]:
        reason = printable(record["reason"])
        self.stopped, self.phase = True, "STOPPED"
        return [f"Receiver stopped: {_STOP_REASONS.get(reason, reason)}."]

    def receiver_exited(
        self, returncode: int, stderr_tail: Iterable[str], requested: bool
    ) -> list[str]:
        """Messages for a child process that has exited; quiet after a clean stop."""
        self.phase = "STOPPED"
        if returncode == 0 and (self.stopped or requested):
            return []
        if returncode == 0:
            messages = ["The receiver stream ended without recon_stopped."]
        else:
            messages = [f"Receiver process exited with status {returncode}."]
        messages.extend(f"  {line}" for line in stderr_tail)
        return [printable(message) for message in messages]

    def exit_failed(self, returncode: int, requested: bool) -> bool:
        """Whether the child ended on its own without a clean ``recon_stopped``.

        A stream that simply ends was cut short, even with exit status 0.
        """
        return not requested and (returncode != 0 or not self.stopped)

    def _confirm(self, key: int, pi: int | None) -> None:
        """Record a confirmed PI; a different one invalidates the tuning's caches."""
        if pi is None:
            return
        if self.identity.get(key, pi) != pi:
            self.hops.pop(key, None)
            self.last_ct.pop(key, None)
        self.identity[key] = pi

    def start_failed(self, error: OSError) -> list[str]:
        self.phase = "STOPPED"
        return [printable(f"Could not start the receiver: {error}")]

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
        status = self.phase
        if self.iteration:
            status += f" #{self.iteration}"
        if self.phase == "ACQUISITION" and self.wide is not None:
            status += f" · WIDE {self.wide[0] / 1e6:.2f} MHz"
        elif self.phase == "ACQUISITION" and self.band is not None:
            start, end, step = self.band
            total = math.floor((end - start) / step + 1e-6) + 1 if step > 0 else 0
            if self.scan_freq_hz is not None:
                status += f" · {self.scan_freq_hz / 1e6:.2f} MHz"
            status += f" · {self.scan_points}/{total} points"
        elif self.phase == "MAINTENANCE":
            status += self._hop_progress() if self.wide is None else self._wide_progress()
        if self.next_rescan_in_s is not None and self.phase != "STOPPED":
            remaining = max(0.0, self.next_rescan_in_s - self.elapsed())
            status += f" · next rescan in {remaining:.0f} s"
        return status

    def _hop_progress(self) -> str:
        if not self.n_stations:
            return " · no stations to visit"
        progress = f" · hop {min(self.hops_done + 1, self.n_stations)}/{self.n_stations}"
        if self.hops_done < min(self.n_stations, len(self.watchlist)):
            progress += f" · recording {self.watchlist[self.hops_done].freq_hz / 1e6:.2f} MHz"
        return progress

    def _wide_progress(self) -> str:
        if not self.n_stations:
            return " · no stations to record"
        progress = f" · WIDE {self.wide[0] / 1e6:.2f} MHz"
        if self.hops_done < self.n_stations:
            return progress + f" · recording {self.n_stations} stations at once"
        return progress + f" · {self.n_stations} stations from one capture"

    def _consensus_view(self) -> ConsensusView | None:
        consensus = self.consensus
        if consensus is None:
            return None
        elapsed = self.elapsed()
        assessed = f"assessed {elapsed:.0f} s ago"
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
                notes=f"the last estimate expired: its CT is {age_s:.0f} s old",
                assessed=assessed,
            )
        outliers = None
        if consensus.outliers_mhz:
            outliers = f"Outliers: {', '.join(f'{f:.2f}' for f in consensus.outliers_mhz)} MHz"
        uncertainty = "?" if consensus.uncertainty_s is None else f"{consensus.uncertainty_s:.0f}"
        utc = age = host_offset = None
        if consensus.utc is not None:
            now = consensus.utc + timedelta(seconds=elapsed)
            utc = f"{now:%Y-%m-%d %H:%M:%S}"
            if age_s is not None:
                age = f"age {age_s:.0f} s"
            delta = (self.wall_clock() - now).total_seconds()
            host_offset = f"Host clock minus consensus: {delta:+.1f} s"
        return ConsensusView(
            trust_level=consensus.trust_level,
            utc=utc,
            uncertainty=f"±{uncertainty} s",
            sources=sources,
            age=age,
            host_offset=host_offset,
            outliers=outliers,
            notes=consensus.notes,
            assessed=assessed,
        )

    def _row(self, station: Station) -> Row:
        key = _tuning(station.freq_hz)
        hop = self.hops.get(key)
        last_ct = self.last_ct.get(key)
        track = self.tracks.get(key)
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
            ct, lto = f"{last_ct.ct_utc:%H:%M}Z", _lto(last_ct.lto_minutes)
        elif hop is not None:
            ct = "none"
        age = trust = "—"
        if track is not None:
            trust = f"{track.trust_score:.2f}"
            if track.age_s is not None:
                age = f"{track.age_s + self.elapsed():.0f} s"
        cells = (f"{station.freq_hz / 1e6:.2f}", ps, _pi(pi), _db(rssi), str(groups), corrected)
        cells += (ct, lto, age, trust)
        attributed = track is not None or key not in self.tracks
        return Row(cells=cells, status=self._station_status(key, hop, track, attributed))

    def _station_status(
        self, key: int, hop: Hop | None, track: Track | None, attributed: bool
    ) -> str:
        if attributed and self.consensus is not None and key in self.consensus.outliers:
            return "OUTLIER"
        if attributed and self.consensus is not None and key in self.consensus.contributing:
            return "ok"
        if track is not None:
            return "not counted"
        if hop is not None and key not in self.last_ct:
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
