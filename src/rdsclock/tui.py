"""Terminal dashboard for ``rdsclock recon`` (optional extra ``[tui]``).

The dashboard never talks to the receiver. It starts ``rdsclock recon
--jsonl`` as a child process and shows the events that the child writes to
stdout; :func:`rdsclock.recon.event_to_json` documents the format. A
separate process keeps the pure-Python DSP loops from stalling the
interface, and SIGINT stops a capture without waiting for it to finish.

Keys: ``q`` stops the child as Ctrl+C would and quits, ``r`` asks for an
acquisition scan after the current cycle (SIGUSR1), and ``p`` pauses the
view while the receiver keeps running.

Records are type-checked before they are applied, and every string shown
comes from broadcasts or the child process: it is reduced to printable
characters and rendered as plain text, never as markup.
"""

import asyncio
import contextlib
import json
import math
import signal
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, RichLog, Static

SUPPORTED_SCHEMA = 1

Record = dict[str, Any]

_STDERR_TAIL_LINES = 20
_LINE_LIMIT_BYTES = 1 << 20  # far above the largest cycle_completed record
_TRUST_STYLES = {
    "HIGH": "bold green",
    "MEDIUM": "yellow",
    "LOW": "dark_orange",
    "STALE": "bold red",
}
_STOP_REASONS = {"iterations": "iteration limit reached", "interrupted": "stopped on request"}


def _printable(value: object) -> str:
    """Keep broadcast and child-process text from controlling the terminal."""
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
    moment = datetime.fromisoformat(_printable(value))
    if moment.tzinfo is None:
        raise ValueError("timestamp without a UTC offset")
    return moment


def _channel(freq_hz: float) -> int:
    """The 100 kHz bin that the consensus also uses to group observations."""
    return round(freq_hz / 100_000)


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
            ps_name=_printable(record["ps_name"]).strip(),
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
            ps_name=_printable(record["ps_name"]).strip(),
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
    """One consensus track: the age of its latest CT and its trust score."""

    age_s: float | None
    trust_score: float


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
            trust_level=_printable(record["trust_level"]),
            n_sources=_integer(record["n_sources"]),
            contributing=frozenset(
                _channel(_number(freq) * 1e6) for freq in record["contributing_freqs_mhz"]
            ),
            outliers=frozenset(_channel(freq * 1e6) for freq in outliers_mhz),
            outliers_mhz=outliers_mhz,
            median_age_s=_optional_number(record["median_age_s"]),
            notes=_printable(record["notes"]),
        )


@dataclass
class DashboardState:
    """Everything the dashboard shows, built from recon events without UI code.

    ``clock`` is the dashboard's own monotonic clock. Between two
    ``cycle_completed`` events it advances the consensus time, the ages and
    the rescan countdown.
    """

    clock: Callable[[], float] = time.monotonic
    phase: str = "STARTING"
    iteration: int = 0
    ready: bool = False
    stopped: bool = False
    watchlist: list[Station] = field(default_factory=list)
    hops: dict[int, Hop] = field(default_factory=dict)
    consensus: Consensus | None = None
    tracks: dict[int, Track] = field(default_factory=dict)
    host_utc: datetime | None = None
    next_rescan_in_s: float | None = None
    cycle_at: float = 0.0
    band: tuple[float, float, float] | None = None
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
        return [_printable(message) for message in messages]

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
        self.phase, self.iteration, self.band = "ACQUISITION", iteration, band
        self.scan_points, self.scan_freq_hz = 0, None
        return [
            f"Acquisition #{iteration}: scanning {band[0]:.2f}–{band[1]:.2f} MHz "
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
        watched = {_channel(station.freq_hz) for station in watchlist}
        self.watchlist = watchlist
        self.hops = {key: hop for key, hop in self.hops.items() if key in watched}
        if not watchlist:
            return ["Watchlist empty: no RDS station found in this scan."]
        freqs = ", ".join(f"{station.freq_hz / 1e6:.2f}" for station in watchlist)
        return [f"Watchlist: {freqs} MHz."]

    def _on_maintenance_started(self, record: Record) -> list[str]:
        iteration, n_stations = _integer(record["iteration"]), _integer(record["n_stations"])
        self.phase = "MAINTENANCE"
        self.iteration, self.n_stations, self.hops_done = iteration, n_stations, 0
        return []

    def _on_hop_result(self, record: Record) -> list[str]:
        freq_hz = _number(record["freq_hz"])
        hop = Hop.from_record(record)
        self.hops[_channel(freq_hz)] = hop
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
        tracks = {
            _channel(_number(track["freq_hz"])): Track(
                age_s=_optional_number(track["age_s"]),
                trust_score=_number(track["trust_score"]),
            )
            for track in record["tracks"]
        }
        next_rescan_in_s = _number(record["next_rescan_in_s"])
        host_utc = _timestamp(record["host_utc"])
        self.consensus, self.tracks, self.watchlist = consensus, tracks, watchlist
        self.next_rescan_in_s, self.host_utc = next_rescan_in_s, host_utc
        self.cycle_at = self.clock()
        return [
            f"Cycle #{iteration}: {new_observations} new CT observation(s); "
            f"trust {consensus.trust_level}, N={consensus.n_sources}."
        ]

    def _on_recon_stopped(self, record: Record) -> list[str]:
        reason = _printable(record["reason"])
        self.stopped, self.phase = True, "STOPPED"
        return [f"Receiver stopped: {_STOP_REASONS.get(reason, reason)}."]

    def receiver_exited(
        self, returncode: int, stderr_tail: Iterable[str], requested: bool
    ) -> list[str]:
        """Messages for a child process that has exited; quiet after a clean stop."""
        self.phase = "STOPPED"
        if returncode == 0 and (self.stopped or requested):
            return []
        messages = [f"Receiver process exited with status {returncode}."]
        messages.extend(f"  {line}" for line in stderr_tail)
        return [_printable(message) for message in messages]

    def start_failed(self, error: OSError) -> list[str]:
        self.phase = "STOPPED"
        return [_printable(f"Could not start the receiver: {error}")]

    def elapsed(self) -> float:
        """Seconds since the latest ``cycle_completed`` event arrived."""
        return 0.0 if self.consensus is None else self.clock() - self.cycle_at

    def status_line(self) -> Text:
        text = Text(self.phase, style="bold")
        if self.iteration:
            text.append(f" #{self.iteration}")
        if self.phase == "ACQUISITION" and self.band is not None:
            start, end, step = self.band
            total = math.floor((end - start) / step + 1e-6) + 1 if step > 0 else 0
            if self.scan_freq_hz is not None:
                text.append(f" · {self.scan_freq_hz / 1e6:.2f} MHz")
            text.append(f" · {self.scan_points}/{total} points")
        elif self.phase == "MAINTENANCE":
            text.append(self._hop_progress())
        if self.next_rescan_in_s is not None and self.phase != "STOPPED":
            remaining = max(0.0, self.next_rescan_in_s - self.elapsed())
            text.append(f" · next rescan in {remaining:.0f} s")
        return text

    def _hop_progress(self) -> str:
        if not self.n_stations:
            return " · no stations to visit"
        progress = f" · hop {min(self.hops_done + 1, self.n_stations)}/{self.n_stations}"
        if self.hops_done < min(self.n_stations, len(self.watchlist)):
            progress += f" · recording {self.watchlist[self.hops_done].freq_hz / 1e6:.2f} MHz"
        return progress

    def consensus_text(self) -> Text:
        """The consensus advanced by the dashboard clock, with its caveats."""
        consensus = self.consensus
        if consensus is None:
            text = Text("Waiting for the first completed cycle…", style="dim")
        elif consensus.utc is None:
            text = Text("TIME UNAVAILABLE", style="bold red")
            text.append("   trust ")
            text.append(consensus.trust_level, style=_TRUST_STYLES.get(consensus.trust_level, ""))
            text.append(f"   {consensus.notes}")
        else:
            elapsed = self.elapsed()
            now = consensus.utc + timedelta(seconds=elapsed)
            uncertainty = (
                "?" if consensus.uncertainty_s is None else f"{consensus.uncertainty_s:.0f}"
            )
            text = Text(f"UTC {now:%Y-%m-%d %H:%M:%S}", style="bold")
            text.append(f"   ±{uncertainty} s   ")
            text.append(consensus.trust_level, style=_TRUST_STYLES.get(consensus.trust_level, ""))
            text.append(f"   N={consensus.n_sources}")
            if consensus.median_age_s is not None:
                text.append(f"   age {consensus.median_age_s + elapsed:.0f} s")
            if self.host_utc is not None:
                delta = (self.host_utc - consensus.utc).total_seconds()
                text.append(f"\nHost clock minus consensus: {delta:+.1f} s")
        if consensus is not None and consensus.outliers_mhz:
            outliers = ", ".join(f"{freq:.2f}" for freq in consensus.outliers_mhz)
            text.append(f"\nOutliers: {outliers} MHz", style="red")
        text.append(
            "\n± is source dispersion and holdover, not a calibrated UTC error.", style="dim"
        )
        return text

    def station_rows(self) -> list[tuple[Text, ...]]:
        """One table row per watchlist station, from scan, hop and consensus data."""
        return [self._row(station) for station in self.watchlist]

    def _row(self, station: Station) -> tuple[Text, ...]:
        channel = _channel(station.freq_hz)
        hop = self.hops.get(channel)
        track = self.tracks.get(channel)
        if hop is None:
            ps, pi, rssi, groups = station.ps_name, station.pi, station.rssi_db, station.n_groups
            corrected = last_ct = lto = ""
        else:
            ps = hop.ps_name or station.ps_name
            pi = station.pi if hop.pi is None else hop.pi
            rssi, groups, corrected = hop.rssi_db, hop.n_groups, str(hop.n_groups_corrected)
            last_ct = "none" if hop.ct_utc is None else f"{hop.ct_utc:%H:%M}Z"
            lto = "" if hop.lto_minutes is None else _lto(hop.lto_minutes)
        age = trust = "—"
        if track is not None:
            trust = f"{track.trust_score:.2f}"
            if track.age_s is not None:
                age = f"{track.age_s + self.elapsed():.0f} s"
        cells = (f"{station.freq_hz / 1e6:.2f}", ps, _pi(pi), _db(rssi), str(groups), corrected)
        cells += (last_ct, lto, age, trust)
        return (*(Text(cell) for cell in cells), self._status(channel, hop, track))

    def _status(self, channel: int, hop: Hop | None, track: Track | None) -> Text:
        if self.consensus is not None and channel in self.consensus.outliers:
            return Text("OUTLIER", style="bold red")
        if self.consensus is not None and channel in self.consensus.contributing:
            return Text("ok", style="green")
        if track is not None:
            return Text("not counted", style="dim")
        if hop is not None and hop.ct_utc is None:
            return Text("no CT", style="dim")
        return Text("")


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


class ReconChild:
    """``rdsclock recon --jsonl`` running as a child process."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._stderr_task = asyncio.create_task(self._keep_stderr_tail())

    @classmethod
    async def start(cls, command: list[str]) -> "ReconChild":
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_LINE_LIMIT_BYTES,
        )
        return cls(process)

    @property
    def running(self) -> bool:
        return self.process.returncode is None

    async def lines(self) -> AsyncIterator[str]:
        """Decoded stdout lines, until the child closes its stdout.

        The reader drops a line longer than its limit; it is passed on as an
        empty line, which the state reports as not JSON.
        """
        while True:
            try:
                line = await self.process.stdout.readline()
            except ValueError:
                yield ""
                continue
            if not line:
                return
            yield line.decode("utf-8", errors="replace")

    async def _keep_stderr_tail(self) -> None:
        # Draining stderr also keeps the child from blocking on a full pipe.
        while line := await self.process.stderr.readline():
            self.stderr_tail.append(line.decode("utf-8", errors="replace").rstrip())

    async def wait(self) -> int:
        returncode = await self.process.wait()
        await self._stderr_task
        return returncode

    def send_signal(self, signum: int) -> bool:
        """Send ``signum`` while the child runs; report whether it was sent."""
        if not self.running:
            return False
        with contextlib.suppress(ProcessLookupError):
            self.process.send_signal(signum)
            return True
        return False

    async def stop(self, timeout_s: float) -> bool:
        """Interrupt the child as Ctrl+C would; kill it after ``timeout_s``.

        Returns ``False`` when the child had to be killed.
        """
        if not self.send_signal(signal.SIGINT):
            return True
        try:
            await asyncio.wait_for(self.process.wait(), timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            await self.process.wait()
            return False
        return True


class ReconDashboard(App[None]):
    """Live view of a ``recon --jsonl`` child process."""

    TITLE = "rdsclock recon"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #status { height: 1; padding: 0 1; background: $panel; }
    #consensus { height: auto; padding: 1 1 0 1; }
    #stations { height: 1fr; margin-top: 1; }
    #log { height: 8; border-top: solid $panel; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "rescan", "Rescan"),
        Binding("p", "pause", "Pause view"),
    ]
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

    stop_timeout_s = 5.0

    def __init__(self, command: list[str], state: DashboardState | None = None) -> None:
        super().__init__()
        self.command = command
        self.state = state if state is not None else DashboardState()
        self.child: ReconChild | None = None
        self.paused = False
        self.failed = False
        self.held: list[str] = []
        self._stop_requested = False

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield Static(id="consensus")
        yield DataTable(id="stations", cursor_type="none", zebra_stripes=True)
        yield RichLog(id="log", max_lines=500)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns(*self.COLUMNS)
        # Live recon rescans on SIGUSR1; the dashboard forwards it instead of dying.
        asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, self.action_rescan)
        self.run_worker(self._follow_receiver(), exclusive=True)
        self.set_interval(1.0, self._refresh)
        self._refresh()

    async def _follow_receiver(self) -> None:
        try:
            self.child = await ReconChild.start(self.command)
        except OSError as exc:
            self.failed = True
            self._log_all(self.state.start_failed(exc))
            self._refresh()
            return
        async for line in self.child.lines():
            self._log_all(self.state.apply_line(line))
            self._refresh()
        returncode = await self.child.wait()
        self.failed = returncode != 0 and not self._stop_requested
        self._log_all(
            self.state.receiver_exited(returncode, self.child.stderr_tail, self._stop_requested)
        )
        self._refresh()

    def _refresh(self) -> None:
        if self.paused:
            return
        self.query_one("#status", Static).update(self.state.status_line())
        self.query_one("#consensus", Static).update(self.state.consensus_text())
        table = self.query_one(DataTable)
        table.clear()
        for row in self.state.station_rows():
            table.add_row(*row)

    def _log_all(self, messages: list[str]) -> None:
        if self.paused:
            self.held.extend(messages)
            return
        log = self.query_one(RichLog)
        for message in messages:
            log.write(Text(message))

    async def _stop_receiver(self) -> None:
        if self.child is None or not self.child.running:
            return
        self._stop_requested = True
        if not await self.child.stop(self.stop_timeout_s):
            self._log_all([f"The receiver did not stop within {self.stop_timeout_s:g} s; killed."])

    async def action_quit(self) -> None:
        """Stop the receiver as Ctrl+C would, then leave."""
        await self._stop_receiver()
        self.exit(return_code=1 if self.failed else 0)

    def action_rescan(self) -> None:
        if self.child is None or not self.state.ready or self.state.stopped:
            self._log_all(["The receiver is not ready for a rescan."])
        elif self.child.send_signal(signal.SIGUSR1):
            self._log_all(["Rescan requested; it starts after the current cycle."])

    def action_pause(self) -> None:
        self.paused = not self.paused
        if self.paused:
            banner = "VIEW PAUSED · the receiver keeps running · p resumes"
            self.query_one("#status", Static).update(Text(banner, style="reverse"))
            return
        held, self.held = self.held, []
        self._log_all(held)
        self._refresh()

    async def on_unmount(self) -> None:
        asyncio.get_running_loop().remove_signal_handler(signal.SIGUSR1)
        # Covers exits that bypass action_quit; never leave the receiver running.
        await self._stop_receiver()


def run_dashboard(command: list[str]) -> int:
    """Run the dashboard until the operator quits; return the exit status.

    The status is 1 when the receiver failed on its own, otherwise the app's.
    """
    app = ReconDashboard(command)
    app.run()
    return 1 if app.failed else (app.return_code or 0)
