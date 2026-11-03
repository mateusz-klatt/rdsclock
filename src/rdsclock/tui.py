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

_TRUST_STYLES = {
    "HIGH": "bold green",
    "MEDIUM": "yellow",
    "LOW": "dark_orange",
    "STALE": "bold red",
}
_STATUS_STYLES = {"OUTLIER": "bold red", "ok": "green", "not counted": "dim", "no CT": "dim"}


def status_text(view: View) -> Text:
    text = Text(view.status)
    text.stylize("bold", 0, len(view.phase))
    return text


def consensus_text(consensus: ConsensusView | None) -> Text:
    """The consensus panel, with the same caveats as the text display."""
    if consensus is None:
        text = Text("Waiting for the first completed cycle…", style="dim")
    elif consensus.utc is None:
        text = Text("TIME UNAVAILABLE", style="bold red")
        text.append("   trust ")
        text.append(consensus.trust_level, style=_TRUST_STYLES.get(consensus.trust_level, ""))
        text.append(f" ({consensus.assessed})", style="dim")
        text.append(f"   {consensus.notes}")
    else:
        text = Text(f"UTC {consensus.utc}", style="bold")
        text.append(f"   {consensus.uncertainty}   ")
        text.append(consensus.trust_level, style=_TRUST_STYLES.get(consensus.trust_level, ""))
        text.append(f" ({consensus.assessed})", style="dim")
        text.append(f"   {consensus.sources}")
        if consensus.age is not None:
            text.append(f"   {consensus.age}")
        if consensus.host_offset is not None:
            text.append(f"\n{consensus.host_offset}")
    if consensus is not None and consensus.outliers is not None:
        text.append(f"\n{consensus.outliers}", style="red")
    text.append(f"\n{UNCERTAINTY_NOTE}", style="dim")
    return text


def row_cells(row: Row) -> tuple[Text, ...]:
    status = Text(row.status, style=_STATUS_STYLES.get(row.status, ""))
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
        self.query_one(DataTable).add_columns(*COLUMNS)
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
        view = self.state.view()
        self.query_one("#status", Static).update(status_text(view))
        self.query_one("#consensus", Static).update(consensus_text(view.consensus))
        table = self.query_one(DataTable)
        table.clear()
        for row in view.rows:
            table.add_row(*row_cells(row))

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
        if self.child is None or not self.state.can_rescan():
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
