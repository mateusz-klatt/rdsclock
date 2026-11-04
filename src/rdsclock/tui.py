"""Terminal dashboard for ``rdsclock recon`` (optional extra ``[tui]``).

The dashboard never talks to the receiver. It starts ``rdsclock recon
--jsonl`` as a child process and shows what :mod:`rdsclock.dashboard` makes
of its events. A separate process keeps the pure-Python DSP loops from
stalling the interface, and SIGINT stops a capture without waiting for it
to finish.

Keys: ``q`` stops the child as Ctrl+C would and quits, ``r`` asks for an
acquisition scan after the current cycle (SIGUSR1), and ``p`` pauses the
view while the receiver keeps running. Every displayed string is rendered
as Rich ``Text``, never as markup.
"""

import asyncio
import contextlib
import signal
from collections import deque
from collections.abc import AsyncIterator

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, RichLog, Static

from .dashboard import (
    COLUMNS,
    LINE_LIMIT_BYTES,
    STDERR_TAIL_LINES,
    UNCERTAINTY_NOTE,
    ConsensusView,
    DashboardState,
    Row,
    View,
)
from .i18n import translate

_TRUST_STYLES = {
    "HIGH": "bold green",
    "MEDIUM": "yellow",
    "LOW": "dark_orange",
    "STALE": "bold red",
}
_STATUS_STYLES = {"OUTLIER": "bold red", "ok": "green", "not counted": "dim", "no CT": "dim"}
LOG_LINES = 500  # also bounds the messages held while the view is paused


async def bounded_lines(reader: asyncio.StreamReader) -> AsyncIterator[str]:
    """Decoded lines until end of file; a line over the stream limit becomes ``""``.

    The reader keeps an overlong line buffered, so the line is dropped through
    its own newline, in reads of at most the limit, before the next line counts.
    """
    while True:
        try:
            line = await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:  # end of file
            if exc.partial:
                yield exc.partial.decode("utf-8", errors="replace")
            return
        except asyncio.LimitOverrunError as exc:
            yield ""
            if not await _drop_line(reader, exc.consumed):
                return
            continue
        yield line.decode("utf-8", errors="replace")


async def _drop_line(reader: asyncio.StreamReader, consumed: int) -> bool:
    """Discard the rest of an overlong line; ``False`` if the stream ended first."""
    while True:
        await reader.read(consumed)
        try:
            await reader.readuntil(b"\n")  # the end of the overlong line
        except asyncio.LimitOverrunError as exc:
            consumed = exc.consumed
        except asyncio.IncompleteReadError:
            return False
        else:
            return True


def status_text(view: View, language: str = "en") -> Text:
    text = Text(view.status)
    text.stylize("bold", 0, len(translate(language, view.phase)))
    return text


def consensus_text(consensus: ConsensusView | None, language: str = "en") -> Text:
    """The consensus panel, with the same caveats as the text display."""
    if consensus is None:
        text = Text(translate(language, "Waiting for the first completed cycle…"), style="dim")
    elif consensus.utc is None:
        text = Text(translate(language, "TIME UNAVAILABLE"), style="bold red")
        text.append("   " + translate(language, "Trust") + " ")
        text.append(
            translate(language, consensus.trust_level),
            style=_TRUST_STYLES.get(consensus.trust_level, ""),
        )
        text.append(f" ({consensus.assessed})", style="dim")
        text.append(f"   {consensus.notes}")
    else:
        text = Text(f"UTC {consensus.utc}", style="bold")
        text.append(f"   {consensus.uncertainty}   ")
        text.append(
            translate(language, consensus.trust_level),
            style=_TRUST_STYLES.get(consensus.trust_level, ""),
        )
        text.append(f" ({consensus.assessed})", style="dim")
        text.append(f"   {consensus.sources}")
        if consensus.age is not None:
            text.append(f"   {consensus.age}")
        if consensus.host_offset is not None:
            text.append(f"\n{consensus.host_offset}")
    if consensus is not None and consensus.outliers is not None:
        text.append(f"\n{consensus.outliers}", style="red")
    text.append("\n" + translate(language, UNCERTAINTY_NOTE), style="dim")
    return text


def row_cells(row: Row, language: str = "en") -> tuple[Text, ...]:
    status = Text(translate(language, row.status), style=_STATUS_STYLES.get(row.status, ""))
    return (*(Text(cell) for cell in row.cells), status)


class ReconChild:
    """``rdsclock recon --jsonl`` running as an asyncio child process."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._stderr_task = asyncio.create_task(self._keep_stderr_tail())

    @classmethod
    async def start(cls, command: list[str]) -> "ReconChild":
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=LINE_LIMIT_BYTES,
        )
        return cls(process)

    @property
    def running(self) -> bool:
        return self.process.returncode is None

    def lines(self) -> AsyncIterator[str]:
        """Decoded stdout lines until end of file; an overlong one becomes ``""``.

        The state reports the empty line as not JSON.
        """
        return bounded_lines(self.process.stdout)

    async def _keep_stderr_tail(self) -> None:
        # Draining stderr also keeps the child from blocking on a full pipe.
        async for line in bounded_lines(self.process.stderr):
            self.stderr_tail.append(line.rstrip())

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
    CONTROL_BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "rescan", "Rescan"),
        Binding("p", "pause", "Pause view"),
    ]

    stop_timeout_s = 5.0

    def __init__(
        self, command: list[str], state: DashboardState | None = None, language: str = "en"
    ) -> None:
        super().__init__()
        self.command = command
        self.state = state if state is not None else DashboardState(language=language)
        self.language = self.state.language
        for binding in self.CONTROL_BINDINGS:
            self.bind(binding.key, binding.action, description=self._t(binding.description))
        self.child: ReconChild | None = None
        self.paused = False
        self.failed = False
        self.held: deque[str] = deque(maxlen=LOG_LINES)
        self._stop_requested = False

    def _t(self, message: str, **values: object) -> str:
        return translate(self.language, message, **values)

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield Static(id="consensus")
        yield DataTable(id="stations", cursor_type="none", zebra_stripes=True)
        yield RichLog(id="log", max_lines=LOG_LINES)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns(*(self._t(name) for name in COLUMNS))
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
        self.failed = self.state.exit_failed(returncode, self._stop_requested)
        self._log_all(
            self.state.receiver_exited(returncode, self.child.stderr_tail, self._stop_requested)
        )
        self._refresh()

    def _refresh(self) -> None:
        if self.paused:
            return
        view = self.state.view()
        self.query_one("#status", Static).update(status_text(view, self.language))
        self.query_one("#consensus", Static).update(consensus_text(view.consensus, self.language))
        table = self.query_one(DataTable)
        table.clear()
        for row in view.rows:
            table.add_row(*row_cells(row, self.language))

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
            self._log_all(
                [
                    self._t(
                        "The receiver did not stop within {timeout:g} s; killed.",
                        timeout=self.stop_timeout_s,
                    )
                ]
            )

    async def action_quit(self) -> None:
        """Stop the receiver as Ctrl+C would, then leave."""
        await self._stop_receiver()
        self.exit(return_code=1 if self.failed else 0)

    def action_rescan(self) -> None:
        if self.child is None or not self.state.can_rescan():
            self._log_all([self._t("The receiver is not ready for a rescan.")])
        elif self.child.send_signal(signal.SIGUSR1):
            self._log_all([self._t("Rescan requested; it starts after the current cycle.")])

    def action_pause(self) -> None:
        self.paused = not self.paused
        if self.paused:
            banner = self._t("VIEW PAUSED · the receiver keeps running · p resumes")
            self.query_one("#status", Static).update(Text(banner, style="reverse"))
            return
        held = list(self.held)
        self.held.clear()
        self._log_all(held)
        self._refresh()

    async def on_unmount(self) -> None:
        asyncio.get_running_loop().remove_signal_handler(signal.SIGUSR1)
        # Covers exits that bypass action_quit; never leave the receiver running.
        await self._stop_receiver()


def run_dashboard(command: list[str], language: str = "en") -> int:
    """Run the dashboard until the operator quits; return the exit status.

    The status is 1 when the receiver failed on its own, otherwise the app's.
    """
    app = ReconDashboard(command, language=language)
    app.run()
    return 1 if app.failed else (app.return_code or 0)
