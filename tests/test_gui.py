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
from tests.fake_receiver import MARKUP_PS, receiver_command  # noqa: E402


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
        assert dashboard.clock.get() == "Waiting for the first completed cycle…"
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
        monkeypatch.setattr(gui, "run_gui", lambda command: commands.append(command) or 0)
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
