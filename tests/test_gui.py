"""Desktop dashboard (``recon --gui``): the Tk window, signals and CLI wiring."""

import contextlib
import gc
import os
import signal
import sys
import time

import pytest

tk = pytest.importorskip("tkinter")

import rdsclock.cli as cli  # noqa: E402
import rdsclock.gui as gui  # noqa: E402
from tests.fake_receiver import (  # noqa: E402
    LIVE,
    MARKUP_PS,
    REPLAY,
    ct_session,
    multi_session,
    receiver_command,
)


@pytest.fixture
def root():
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display for Tk: {exc}")
    window.withdraw()
    yield window
    with contextlib.suppress(tk.TclError):
        window.destroy()
    # Collect the dashboards' Tk variables now, in the main thread. Left to a
    # later collection in another test's background thread, their __del__
    # raises, and -W error turns that into a failure there.
    gc.collect()


def _pump(window, condition, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "the window did not reach the expected state"
        window.update()
        time.sleep(0.01)


def _rows(dashboard: gui.DesktopDashboard) -> list[tuple]:
    table = dashboard.table
    return [(table.item(item, "values"), table.item(item, "tags")) for item in table.get_children()]


class TestDesktopDashboard:
    def test_window_follows_the_receiver_and_stops_it_on_quit(self, root, tmp_path):
        dashboard = gui.DesktopDashboard(root, receiver_command(tmp_path))
        _pump(root, lambda: dashboard.state.consensus is not None)
        assert dashboard.clock.get().startswith("UTC 2026-11-03 12:00:3")
        assert dashboard.trust_label.cget("text") == "HIGH"
        assert dashboard.outliers.get() == "Outliers: 107.50 MHz"
        rows = _rows(dashboard)
        assert [values[0] for values, _ in rows] == ["102.40", "98.80", "107.50"]
        assert rows[2][0][1] == MARKUP_PS
        assert rows[2][1] == ("outlier",)
        assert "Receiver ready: tuner type 5, 29 gain steps." in dashboard.log_text()
        dashboard.quit()
        assert dashboard.receiver.process.returncode == 0
        assert not dashboard.failed

    def test_rescan_button_reaches_the_receiver(self, root, tmp_path):
        dashboard = gui.DesktopDashboard(root, receiver_command(tmp_path))
        _pump(root, lambda: dashboard.state.ready)
        root.update()
        assert dashboard.rescan_button.instate(["!disabled"])
        dashboard.rescan_button.invoke()
        _pump(root, lambda: dashboard.state.iteration == 2)
        assert "Rescan requested; it starts after the current cycle." in dashboard.log_text()
        assert root.bind("<Key-r>")
        dashboard.quit()

    def test_rescan_waits_until_the_receiver_is_ready(self, root):
        command = [sys.executable, "-I", "-c", "import time; time.sleep(30)"]
        dashboard = gui.DesktopDashboard(root, command)
        dashboard.rescan()
        assert "The receiver is not ready for a rescan." in dashboard.log_text()
        assert dashboard.rescan_button.instate(["disabled"])
        dashboard.quit()

    def test_paused_view_holds_updates_until_resumed(self, root, tmp_path):
        dashboard = gui.DesktopDashboard(root, receiver_command(tmp_path))
        _pump(root, lambda: dashboard.state.consensus is not None)
        dashboard.toggle_pause()
        assert dashboard.status.get().startswith("VIEW PAUSED")
        dashboard.rescan()
        _pump(root, lambda: dashboard.state.iteration == 2)
        assert dashboard.status.get().startswith("VIEW PAUSED")
        assert any(message.startswith("Acquisition #2") for message in dashboard.held)
        dashboard.toggle_pause()
        assert not dashboard.held
        assert dashboard.status.get().startswith("ACQUISITION #2")
        assert dashboard.pause_button.cget("text") == "Pause view"
        dashboard.quit()

    def test_receiver_failure_is_shown(self, root, tmp_path):
        dashboard = gui.DesktopDashboard(root, receiver_command(tmp_path, "fail"))
        _pump(root, lambda: dashboard.state.phase == "STOPPED")
        assert "Receiver process exited with status 1." in dashboard.log_text()
        assert "Connection refused" in dashboard.log_text()
        assert dashboard.failed
        dashboard.quit()

    def test_stream_ending_without_a_stop_is_a_failure(self, root, tmp_path):
        dashboard = gui.DesktopDashboard(root, receiver_command(tmp_path, "truncated"))
        assert dashboard.held.maxlen == gui.LOG_LINES
        _pump(root, lambda: dashboard.state.phase == "STOPPED")
        assert "The receiver stream ended without recon_stopped." in dashboard.log_text()
        assert dashboard.failed
        dashboard.quit()

    def test_missing_receiver_command_is_reported(self, root, tmp_path):
        dashboard = gui.DesktopDashboard(root, [str(tmp_path / "missing-rdsclock")])
        assert dashboard.log_text().startswith("Could not start the receiver")
        assert dashboard.failed
        dashboard.quit()

    def test_signals_request_a_rescan_and_close_the_window(self, root, tmp_path):
        before = signal.getsignal(signal.SIGINT)
        dashboard = gui.DesktopDashboard(root, receiver_command(tmp_path))
        _pump(root, lambda: dashboard.state.ready)
        os.kill(os.getpid(), signal.SIGUSR1)
        _pump(root, lambda: dashboard.state.iteration == 2)
        os.kill(os.getpid(), signal.SIGINT)  # handled by the window, not KeyboardInterrupt
        _pump(root, lambda: dashboard.closed)
        assert dashboard.receiver.process.returncode == 0
        assert signal.getsignal(signal.SIGINT) is before

    def test_log_keeps_the_latest_lines(self, root, monkeypatch, tmp_path):
        monkeypatch.setattr(gui, "LOG_LINES", 3)
        dashboard = gui.DesktopDashboard(root, [str(tmp_path / "missing-rdsclock")])
        dashboard._log([f"line {n}" for n in range(5)])
        assert dashboard.log_text().splitlines() == ["line 2", "line 3", "line 4"]
        dashboard.quit()

    def test_timers_stop_once_the_window_closes(self, root, tmp_path):
        dashboard = gui.DesktopDashboard(root, [str(tmp_path / "missing-rdsclock")])
        dashboard._tick()  # refreshes, then schedules the next tick
        dashboard.close()
        dashboard.close()  # closing twice is harmless
        dashboard._poll()
        dashboard._tick()
        assert dashboard.closed

    def test_consensus_panel_states(self, root, tmp_path):
        dashboard = gui.DesktopDashboard(root, [str(tmp_path / "missing-rdsclock")])
        assert dashboard.clock.get() == "Waiting for the first completed cycle..."
        expired = gui.ConsensusView("STALE", None, "±? s", "N=2", None, None, None, "expired", "")
        dashboard._show_consensus(expired)
        assert (dashboard.clock.get(), dashboard.facts.get()) == ("TIME UNAVAILABLE", "expired")
        dashboard.quit()


class TestRunGui:
    def test_closes_and_reports_the_status(self, monkeypatch, tmp_path):
        try:
            tk.Tk().destroy()
        except tk.TclError as exc:
            pytest.skip(f"no display for Tk: {exc}")
        monkeypatch.setattr(tk.Tk, "mainloop", lambda self, n=0: None)
        assert gui.run_gui(receiver_command(tmp_path)) == 0
        assert gui.run_gui([str(tmp_path / "missing-rdsclock")]) == 1

    def test_without_a_display(self, monkeypatch, capsys):
        def no_display():
            raise tk.TclError("no display name and no $DISPLAY environment variable")

        monkeypatch.setattr(gui.tk, "Tk", no_display)
        assert gui.run_gui(["rdsclock"]) == 2
        assert "cannot open a window" in capsys.readouterr().err


class TestReconGuiCli:
    def test_child_runs_jsonl_with_the_same_settings(self, monkeypatch):
        commands: list[list[str]] = []
        monkeypatch.setattr(
            gui, "run_gui", lambda command, language="en": commands.append(command) or 0
        )
        assert cli.main(["recon", "--gui", "--start=98"]) == 0
        ((command,),) = [commands]
        assert command[:5] == [sys.executable, "-m", "rdsclock", "recon", "--jsonl"]
        assert "--start=98.0" in command

    def test_offline_replay_is_rejected(self, tmp_path, capsys):
        assert cli.main(["recon", "--gui", "--from-dir", str(tmp_path)]) == 2
        assert "--from-dir" in capsys.readouterr().err

    def test_broken_tkinter_is_explained(self, monkeypatch, capsys):
        # Present but unusable, e.g. without the native _tkinter extension.
        monkeypatch.setitem(sys.modules, "rdsclock.gui", None)
        assert cli.main(["recon", "--gui"]) == 2
        assert "python3-tk" in capsys.readouterr().err


@pytest.mark.parametrize(
    "language,trust,rescan,paused,groups",
    [
        ("en", "HIGH", "Rescan", "VIEW PAUSED", "Groups"),
        ("pl", "WYSOKIE", "Skanuj ponownie", "WIDOK WSTRZYMANY", "Grupy"),
        ("fr", "ÉLEVÉE", "Relancer le balayage", "AFFICHAGE SUSPENDU", "Groupes"),
    ],
)
def test_window_language_reaches_controls_table_and_status(
    root, tmp_path, language, trust, rescan, paused, groups
):
    dashboard = gui.DesktopDashboard(root, receiver_command(tmp_path), language=language)
    try:
        _pump(root, lambda: dashboard.state.consensus is not None)
        assert dashboard.trust_label.cget("text") == trust
        assert dashboard.rescan_button.cget("text") == rescan
        assert dashboard.table.heading("Groups", "text") == groups
        assert _rows(dashboard)[2][0][1] == MARKUP_PS
        assert _rows(dashboard)[2][1] == ("outlier",)
        dashboard.toggle_pause()
        assert dashboard.status.get().startswith(paused)
    finally:
        dashboard.quit()


def _punctuation_tk_cannot_draw(text: str) -> set[str]:
    return {char for char in text if 0x2000 <= ord(char) <= 0x206F}


@pytest.mark.parametrize("language", ["en", "pl", "fr"])
def test_waiting_text_avoids_a_glyph_the_fixed_font_lacks(root, tmp_path, language):
    dashboard = gui.DesktopDashboard(root, receiver_command(tmp_path), language=language)
    try:
        waiting = dashboard.clock.get()
        assert waiting.endswith("...")
        assert not _punctuation_tk_cannot_draw(waiting)
    finally:
        dashboard.quit()


def test_every_widget_spells_general_punctuation_in_ascii(root, tmp_path):
    # Tk's fonts draw U+2019 and U+2014 as nothing: "l’hôte" became "lhôte".
    dashboard = gui.DesktopDashboard(root, receiver_command(tmp_path), language="fr")
    try:
        _pump(root, lambda: dashboard.state.consensus is not None)
        dashboard._refresh()
        assert dashboard.pause_button.cget("text") == "Suspendre l'affichage"
        assert "l'hôte" in dashboard.offset.get()
        cells = [
            str(v)
            for item in dashboard.table.get_children()
            for v in dashboard.table.item(item, "values")
        ]
        shown = [
            dashboard.status.get(),
            dashboard.clock.get(),
            dashboard.facts.get(),
            dashboard.offset.get(),
            dashboard.outliers.get(),
            dashboard.assessed_label.cget("text"),
            dashboard.log_text(),
            *cells,
        ]
        assert not _punctuation_tk_cannot_draw("".join(shown))
    finally:
        dashboard.quit()


def test_tk_text_replaces_only_what_the_fonts_lack():
    assert gui.tk_text("l’hôte — 2–3 “x” „y” ‘z’…") == 'l\'hôte - 2-3 "x" "y" \'z\'...'
    assert gui.tk_text("12:00Z ← Europe/Warsaw → 11:00Z") == "12:00Z <- Europe/Warsaw -> 11:00Z"
    assert gui.tk_text("Zażółć · ±1 s · «»") == "Zażółć · ±1 s · «»"


def test_table_shows_ct_read_as_local_time_in_a_wide_enough_column(root, tmp_path):
    dashboard = gui.DesktopDashboard(root, receiver_command(tmp_path, records=ct_session()))
    try:
        _pump(root, lambda: dashboard.state.consensus is not None)
        dashboard._refresh()
        rows = _rows(dashboard)
        assert rows[0][0][6] == "12:00Z <- Europe/Warsaw"
        assert rows[2][0][6] == "02:30 Europe/Warsaw: ambiguous, not used"
        width = int(dashboard.table.column("Last CT", "width"))
        assert width >= dashboard.cell_font.measure(rows[2][0][6])
        assert "read as Europe/Warsaw local time = 12:00Z" in dashboard.log_text()
    finally:
        dashboard.quit()


def test_standard_rows_keep_the_column_layout(root, tmp_path):
    dashboard = gui.DesktopDashboard(root, receiver_command(tmp_path))
    try:
        _pump(root, lambda: dashboard.state.consensus is not None)
        dashboard._refresh()
        assert {int(dashboard.table.column(name, "width")) for name in gui.COLUMNS} == {78}
    finally:
        dashboard.quit()


@pytest.mark.parametrize("language,receiver", [("en", "Receiver"), ("fr", "Récepteur")])
def test_several_receivers_add_a_column_status_and_log_lines(root, tmp_path, language, receiver):
    command = receiver_command(tmp_path, records=multi_session())
    dashboard = gui.DesktopDashboard(root, command, language=language)
    try:
        _pump(root, lambda: dashboard.state.iteration == 2 and dashboard.state.hops_done == 1)
        dashboard._refresh()
        assert dashboard.columns == ("Receiver", *gui.COLUMNS)
        assert tuple(dashboard.table["columns"]) == dashboard.columns
        assert dashboard.table.heading("Receiver", "text") == receiver
        rows = _rows(dashboard)
        assert [values[:2] for values, _ in rows] == [(LIVE, "107.50"), (REPLAY, "98.80")]
        width = int(dashboard.table.column("Receiver", "width"))
        assert width >= dashboard.cell_font.measure(LIVE)
        # Tk draws the arrow in ASCII.
        assert f"{REPLAY} -> 98.80 MHz" in dashboard.status.get()
        log = dashboard.log_text()
        assert "connection closed by rtl_tcp" in log
        assert len([text for text in log.splitlines() if REPLAY in text]) >= 3
    finally:
        dashboard.quit()
