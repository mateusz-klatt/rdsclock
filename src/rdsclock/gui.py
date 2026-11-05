"""Desktop dashboard for ``rdsclock recon`` (tkinter, standard library).

``rdsclock recon --gui`` starts ``rdsclock recon --jsonl`` as a child process
and shows what :mod:`rdsclock.dashboard` makes of its events in a window: the
consensus clock, the station table and a log, with *Rescan*, *Pause view* and
*Quit* (keys ``r``, ``p`` and ``q``). Closing the window, or Ctrl+C in the
terminal that started it, stops the receiver as Ctrl+C would; a SIGUSR1 sent
to the window's process requests a rescan.

The reader threads only queue what they receive, and signal handlers only set
counters: every widget is touched from the Tk main loop.
"""

import contextlib
import queue
import signal
import sys
import tkinter as tk
from collections import deque
from tkinter import font as tkfont
from tkinter import ttk

from .dashboard import (
    COLUMNS,
    RECEIVER_COLUMN,
    UNCERTAINTY_NOTE,
    ConsensusView,
    DashboardState,
    ReceiverProcess,
)
from .i18n import translate

POLL_MS = 100
TICK_MS = 1000
LOG_LINES = 500
STOP_TIMEOUT_S = 5.0

_TRUST_COLOURS = {"HIGH": "#1d7f37", "MEDIUM": "#9a6200", "LOW": "#b9491a", "STALE": "#b8232a"}
_STATUS_TAGS = {"OUTLIER": "outlier", "ok": "ok", "not counted": "muted", "no CT": "muted"}
_MUTED = "#5b6475"
_ALERT = "#b8232a"

# Tk's Nimbus fonts have no glyphs for these General Punctuation characters:
# they draw nothing, or unrelated symbols for U+2026, so the window uses ASCII.
# The arrows that mark CT read as local time are spelled in ASCII as well.
_TK_ASCII = str.maketrans(
    {
        "…": "...",
        "’": "'",
        "‘": "'",
        "—": "-",
        "–": "-",
        "“": '"',
        "”": '"',
        "„": '"',
        "←": "<-",
        "→": "->",
    }
)
_COLUMN_WIDTH = 78
_CT_COLUMN = "Last CT"


def tk_text(text: str) -> str:
    """``text`` with the punctuation that Tk's fonts cannot draw spelled in ASCII."""
    return text.translate(_TK_ASCII)


class DesktopDashboard:
    """A Tk window over a ``recon --jsonl`` child process."""

    def __init__(
        self,
        root: tk.Tk,
        command: list[str],
        state: DashboardState | None = None,
        language: str = "en",
    ) -> None:
        self.root = root
        self.state = state if state is not None else DashboardState(language=language)
        self.language = self.state.language
        self.receiver = ReceiverProcess(command, self._queue_line, self._queue_exit)
        self.inbox: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
        self.paused = False
        self.failed = False
        self.closed = False
        self.held: deque[str] = deque(maxlen=LOG_LINES)
        self._stop_requested = False
        self._signals = {signal.SIGUSR1: 0, signal.SIGINT: 0}
        self._handled = dict(self._signals)
        self._build()
        self._previous_handlers = {
            signum: signal.signal(signum, self._count_signal) for signum in self._signals
        }
        root.protocol("WM_DELETE_WINDOW", self.quit)
        for key, action in (("r", self.rescan), ("p", self.toggle_pause), ("q", self.quit)):
            root.bind(f"<Key-{key}>", lambda event, action=action: action())
        try:
            self.receiver.start()
        except OSError as exc:
            self.failed = True
            self._log(self.state.start_failed(exc))
        self._refresh()
        root.after(POLL_MS, self._poll)
        root.after(TICK_MS, self._tick)

    def _t(self, message: str, **values: object) -> str:
        return tk_text(translate(self.language, message, **values))

    def _build(self) -> None:
        root = self.root
        root.title("rdsclock recon")
        root.minsize(760, 520)
        fixed = tkfont.nametofont("TkFixedFont")
        clock_font = tkfont.Font(root, family=fixed.actual("family"), size=26, weight="bold")
        bold = tkfont.Font(root, family=fixed.actual("family"), weight="bold")
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)

        header = ttk.Frame(frame)
        header.pack(fill="x")
        self.status = tk.StringVar()
        ttk.Label(header, textvariable=self.status, font=fixed).pack(
            side="left", expand=True, anchor="w"
        )
        self.rescan_button = ttk.Button(header, text=self._t("Rescan"), command=self.rescan)
        self.pause_button = ttk.Button(
            header, text=self._t("Pause view"), command=self.toggle_pause
        )
        quit_button = ttk.Button(header, text=self._t("Quit"), command=self.quit)
        for button in (quit_button, self.pause_button, self.rescan_button):
            button.pack(side="right", padx=(6, 0))

        panel = ttk.LabelFrame(frame, text=self._t("Consensus"), padding=10)
        panel.pack(fill="x", pady=(10, 0))
        self.clock = tk.StringVar()
        self.clock_label = ttk.Label(panel, textvariable=self.clock, font=clock_font)
        self.clock_label.pack(anchor="w")
        facts = ttk.Frame(panel)
        facts.pack(anchor="w", pady=(4, 0))
        self.trust_label = ttk.Label(facts, font=bold)
        self.trust_label.pack(side="left")
        self.assessed_label = ttk.Label(facts, foreground=_MUTED)
        self.assessed_label.pack(side="left", padx=(8, 0))
        self.facts = tk.StringVar()
        ttk.Label(facts, textvariable=self.facts, font=fixed).pack(side="left", padx=(12, 0))
        self.offset = tk.StringVar()
        ttk.Label(panel, textvariable=self.offset, font=fixed).pack(anchor="w", pady=(4, 0))
        self.outliers = tk.StringVar()
        ttk.Label(panel, textvariable=self.outliers, font=fixed, foreground=_ALERT).pack(anchor="w")
        ttk.Label(panel, text=self._t(UNCERTAINTY_NOTE), foreground=_MUTED).pack(
            anchor="w", pady=(4, 0)
        )

        stations = ttk.LabelFrame(frame, text=self._t("Stations"), padding=10)
        stations.pack(fill="both", expand=True, pady=(10, 0))
        self.table = ttk.Treeview(stations, show="headings", height=7)
        self.columns: tuple[str, ...] = ()
        self._set_columns(COLUMNS)
        self.cell_font = tkfont.nametofont("TkDefaultFont")
        self.table.tag_configure("ok", foreground=_TRUST_COLOURS["HIGH"])
        self.table.tag_configure("outlier", foreground=_ALERT)
        self.table.tag_configure("muted", foreground=_MUTED)
        self.table.pack(fill="both", expand=True)

        history = ttk.LabelFrame(frame, text=self._t("Log"), padding=10)
        history.pack(fill="both", pady=(10, 0))
        self.log = tk.Text(history, height=8, wrap="none", font=fixed, state="disabled")
        scrollbar = ttk.Scrollbar(history, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    def _queue_line(self, line: str) -> None:
        self.inbox.put(("line", line))  # reader thread

    def _queue_exit(self, returncode: int, stderr_tail: list[str]) -> None:
        self.inbox.put(("exit", (returncode, stderr_tail)))  # reader thread

    def _count_signal(self, signum: int, frame: object) -> None:
        self._signals[signum] += 1  # only counts: _poll acts on it

    def _poll(self) -> None:
        """Apply what the readers queued and the signals that arrived; repeat."""
        if self.closed:
            return
        changed = False
        while True:
            try:
                kind, payload = self.inbox.get_nowait()
            except queue.Empty:
                break
            changed = True
            if kind == "line":
                self._log(self.state.apply_line(payload))
            else:
                returncode, stderr_tail = payload
                self.failed = self.state.exit_failed(returncode, self._stop_requested)
                self._log(self.state.receiver_exited(returncode, stderr_tail, self._stop_requested))
        if self._take_signal(signal.SIGINT):
            self.quit()
            return
        if self._take_signal(signal.SIGUSR1):
            self.rescan()
        if changed:
            self._refresh()
        self.root.after(POLL_MS, self._poll)

    def _take_signal(self, signum: int) -> bool:
        received = self._signals[signum]
        if received == self._handled[signum]:
            return False
        self._handled[signum] = received
        return True

    def _tick(self) -> None:
        if self.closed:
            return
        self._refresh()
        self.root.after(TICK_MS, self._tick)

    def _refresh(self) -> None:
        if self.paused:
            return
        view = self.state.view()
        self.status.set(tk_text(view.status))
        self._show_consensus(view.consensus)
        self._set_columns(self.state.columns())
        self.table.delete(*self.table.get_children())
        fitted = [name for name in (RECEIVER_COLUMN, _CT_COLUMN) if name in self.columns]
        for row in view.rows:
            tag = _STATUS_TAGS.get(row.status)
            cells = (*(tk_text(cell) for cell in row.cells), self._t(row.status))
            self.table.insert("", "end", values=cells, tags=(tag,) if tag else ())
            for name in fitted:
                self._fit_column(name, cells[self.columns.index(name)])
        ready = self.state.can_rescan() and self.receiver.running
        self.rescan_button.state(["!disabled"] if ready else ["disabled"])

    def _set_columns(self, columns: tuple[str, ...]) -> None:
        """Lay out the table for ``columns``: a receiver column joins with a second receiver."""
        if columns == self.columns:
            return
        self.table.configure(columns=columns)
        for name in columns:
            self.table.heading(name, text=self._t(name), anchor="w")
            self.table.column(name, width=_COLUMN_WIDTH, minwidth=48, stretch=True, anchor="w")
        self.columns = columns

    def _fit_column(self, name: str, text: str) -> None:
        """Widen a column for a cell that would be cut off; never narrow it."""
        needed = self.cell_font.measure(text) + 16
        if needed > int(self.table.column(name, "width")):
            self.table.column(name, width=needed)

    def _show_consensus(self, consensus: ConsensusView | None) -> None:
        clock, trust, assessed, facts, offset, outliers = "", "", "", "", "", ""
        if consensus is None:
            clock = self._t("Waiting for the first completed cycle…")
        else:
            trust, assessed, outliers = (
                consensus.trust_level,
                consensus.assessed,
                consensus.outliers,
            )
            if consensus.utc is None:
                clock, facts = self._t("TIME UNAVAILABLE"), consensus.notes
            else:
                clock = f"UTC {consensus.utc}"
                parts = (consensus.uncertainty, consensus.sources, consensus.age)
                facts = "   ".join(part for part in parts if part)
                offset = consensus.host_offset or ""
        self.clock.set(tk_text(clock))
        alert = consensus is not None and (consensus.utc is None or trust == "STALE")
        self.clock_label.configure(foreground=_ALERT if alert else "")
        self.trust_label.configure(text=self._t(trust), foreground=_TRUST_COLOURS.get(trust, ""))
        self.assessed_label.configure(text=tk_text(assessed))
        self.facts.set(tk_text(facts))
        self.offset.set(tk_text(offset))
        self.outliers.set(tk_text(outliers or ""))

    def _log(self, messages: list[str]) -> None:
        if self.paused:
            self.held.extend(messages)
            return
        if not messages:
            return
        self.log.configure(state="normal")
        for message in messages:
            self.log.insert("end", tk_text(message) + "\n")
        excess = int(self.log.index("end-1c").split(".")[0]) - 1 - LOG_LINES
        if excess > 0:
            self.log.delete("1.0", f"{excess + 1}.0")
        self.log.configure(state="disabled")
        self.log.see("end")

    def log_text(self) -> str:
        return self.log.get("1.0", "end-1c")

    def rescan(self) -> None:
        if not self.state.can_rescan() or not self.receiver.send_signal(signal.SIGUSR1):
            self._log([self._t("The receiver is not ready for a rescan.")])
            return
        self._log([self._t("Rescan requested; it starts after the current cycle.")])

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause_button.configure(text=self._t("Resume view" if self.paused else "Pause view"))
        if self.paused:
            self.status.set(self._t("VIEW PAUSED · the receiver keeps running"))
            return
        held = list(self.held)
        self.held.clear()
        self._log(held)
        self._refresh()

    def close(self) -> None:
        """Stop the receiver as Ctrl+C would and restore the signal handlers."""
        if self.closed:
            return
        self.closed = True
        self._stop_requested = True
        self.receiver.stop(STOP_TIMEOUT_S)
        self.receiver.join(STOP_TIMEOUT_S)
        for signum, handler in self._previous_handlers.items():
            signal.signal(signum, handler)

    def quit(self) -> None:
        self.close()
        self.root.destroy()


def run_gui(command: list[str], language: str = "en") -> int:
    """Show the window until it is closed; return the exit status."""
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(
            translate(language, "error: cannot open a window: {error}", error=exc), file=sys.stderr
        )
        return 2
    dashboard = DesktopDashboard(root, command, language=language)
    try:
        root.mainloop()
    finally:
        dashboard.close()
        with contextlib.suppress(tk.TclError):
            root.destroy()
    return 1 if dashboard.failed else 0
