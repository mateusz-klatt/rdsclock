"""Terminal dashboard (``recon --tui``): rendering, Textual app and CLI wiring."""

import asyncio
import os
import signal
import sys
import time

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, RichLog, Static  # noqa: E402

import rdsclock.cli as cli  # noqa: E402
import rdsclock.tui as tui  # noqa: E402
from rdsclock.dashboard import DashboardState, View  # noqa: E402
from tests.fake_receiver import (  # noqa: E402
    HOST_UTC,
    MARKUP_PS,
    ct_session,
    cycle,
    line,
    receiver_command,
    session,
)


def _view(lines: list[str]) -> View:
    state = DashboardState(clock=lambda: 100.0, wall_clock=lambda: HOST_UTC)
    for record in lines:
        state.apply_line(record)
    return state.view()


class TestRendering:
    def test_consensus_panel_text_and_styles(self):
        text = tui.consensus_text(_view(session()).consensus)
        assert text.plain.splitlines() == [
            "UTC 2026-11-03 12:00:30   \u00b13 s   HIGH (assessed 0 s ago)   N=2   age 41 s",
            "Host clock minus consensus: +10.0 s",
            "Outliers: 107.50 MHz",
            "\u00b1 is source dispersion and holdover, not a calibrated UTC error.",
        ]
        assert any(span.style == "bold green" for span in text.spans)

    def test_unavailable_and_waiting_panels(self):
        unavailable = tui.consensus_text(_view([*session()[:-1], line(cycle(utc=None))]).consensus)
        assert unavailable.plain.startswith(
            "TIME UNAVAILABLE   Trust STALE (assessed 0 s ago)   no active sources"
        )
        assert tui.consensus_text(None).plain.startswith("Waiting for the first completed cycle")

    def test_ct_read_as_local_time_is_marked_in_plain_cells(self):
        cells = [tui.row_cells(row) for row in _view(ct_session()).rows]
        assert cells[0][6].plain == "12:00Z ← Europe/Warsaw"
        assert cells[2][6].plain == "02:30 Europe/Warsaw: ambiguous, not used"
        assert not cells[0][6].spans
        standard = [tui.row_cells(row) for row in _view(session()).rows]
        assert [cell.plain for cell in cells[1]] == [cell.plain for cell in standard[1]]

    def test_status_line_phase_is_bold(self):
        view = _view(session())
        text = tui.status_text(view)
        assert text.plain == view.status
        assert (text.spans[0].start, text.spans[0].end) == (0, len("MAINTENANCE"))

    def test_cells_are_plain_text_with_status_styles(self):
        cells = tui.row_cells(_view(session()).rows[2])
        assert cells[1].plain == MARKUP_PS
        assert not cells[1].spans
        assert (cells[-1].plain, cells[-1].style) == ("OUTLIER", "bold red")


class _ClosedStream:
    async def readuntil(self, separator: bytes) -> bytes:
        raise asyncio.IncompleteReadError(b"", None)


class _VanishedProcess:
    """A child that is gone by the time a signal is sent."""

    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode
        self.stderr = _ClosedStream()

    def send_signal(self, signum: int) -> None:
        raise ProcessLookupError

    async def wait(self) -> int | None:
        return self.returncode


class TestReconChild:
    def test_signals_are_not_sent_to_a_finished_child(self):
        async def scenario() -> None:
            vanished = tui.ReconChild(_VanishedProcess(returncode=None))
            assert not vanished.send_signal(signal.SIGUSR1)
            assert await vanished.stop(timeout_s=0.1)
            exited = tui.ReconChild(_VanishedProcess(returncode=0))
            assert not exited.send_signal(signal.SIGUSR1)
            await vanished.wait()
            await exited.wait()

        asyncio.run(scenario())


def _read_all(chunks: list[bytes], limit: int = 16) -> list[str]:
    """Feed ``chunks`` one at a time to a bounded reader and collect its lines."""

    async def scenario() -> list[str]:
        reader = asyncio.StreamReader(limit=limit)
        lines: list[str] = []

        async def collect() -> None:
            lines.extend([line async for line in tui.bounded_lines(reader)])

        task = asyncio.create_task(collect())
        for chunk in chunks:
            reader.feed_data(chunk)
            for _ in range(5):
                await asyncio.sleep(0)
        reader.feed_eof()
        await task
        return lines

    return asyncio.run(scenario())


class TestBoundedLines:
    def test_overlong_line_is_dropped_through_its_newline(self):
        record = b'{"event":"x"}\n'
        assert _read_all([b"ok\n" + b"x" * 40 + b"\n" + record]) == ["ok\n", "", record.decode()]

    def test_suffix_arriving_later_is_not_a_record(self):
        record = b'{"event":"y"}\n'
        lines = _read_all([b"x" * 40, b"x" * 40, b'{"event":"suffix"}\n' + record])
        assert lines == ["", record.decode()]

    def test_end_of_file_inside_an_overlong_line_or_after_a_partial_one(self):
        assert _read_all([b"x" * 40]) == [""]
        assert _read_all([b"no newline"]) == ["no newline"]


def _dashboard(tmp_path, mode: str = "follow") -> tui.ReconDashboard:
    return tui.ReconDashboard(receiver_command(tmp_path, mode))


def _drive(app: tui.ReconDashboard, scenario) -> None:
    async def run() -> None:
        async with app.run_test(size=(120, 36)) as pilot:
            await scenario(pilot)

    asyncio.run(run())


async def _until(pilot, condition, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "the dashboard did not reach the expected state"
        await pilot.pause(0.05)


def _log_text(app: tui.ReconDashboard) -> str:
    return "\n".join(line.text for line in app.query_one(RichLog).lines)


class TestReconDashboard:
    def test_follows_the_receiver_and_stops_it_on_quit(self, tmp_path):
        app = _dashboard(tmp_path)

        async def scenario(pilot):
            await _until(pilot, lambda: app.state.consensus is not None)
            await pilot.pause(0.05)
            table = app.query_one(DataTable)
            assert table.row_count == 3
            assert [cell.plain for cell in table.get_row_at(2)][1] == MARKUP_PS
            assert "UTC 2026-11-03 12:00:3" in str(app.query_one("#consensus", Static).content)
            await pilot.press("q")

        _drive(app, scenario)
        assert app.return_code == 0
        assert app.child.process.returncode == 0

    def test_rescan_key_signals_the_receiver(self, tmp_path):
        app = _dashboard(tmp_path)

        async def scenario(pilot):
            await _until(pilot, lambda: app.state.consensus is not None)
            await pilot.press("r")
            await _until(pilot, lambda: app.state.iteration == 2)
            assert app.state.phase == "ACQUISITION"
            assert "Rescan requested; it starts after the current cycle." in _log_text(app)
            await pilot.press("q")

        _drive(app, scenario)
        assert app.return_code == 0

    def test_sigusr1_to_the_dashboard_is_forwarded(self, tmp_path):
        app = _dashboard(tmp_path)

        async def scenario(pilot):
            await _until(pilot, lambda: app.state.consensus is not None)
            os.kill(os.getpid(), signal.SIGUSR1)
            await _until(pilot, lambda: app.state.iteration == 2)
            assert "Rescan requested; it starts after the current cycle." in _log_text(app)
            await pilot.press("q")

        _drive(app, scenario)
        assert app.return_code == 0

    def test_overlong_line_is_skipped(self, tmp_path):
        app = _dashboard(tmp_path, mode="overlong")

        async def scenario(pilot):
            await _until(pilot, lambda: app.state.consensus is not None)
            assert "Ignored a line that is not JSON." in _log_text(app)
            await pilot.press("q")

        _drive(app, scenario)
        assert app.return_code == 0

    def test_rescan_waits_until_the_receiver_is_ready(self, tmp_path):
        app = tui.ReconDashboard([sys.executable, "-I", "-c", "import time; time.sleep(30)"])

        async def scenario(pilot):
            await _until(pilot, lambda: app.child is not None)
            await pilot.press("r")
            await pilot.pause(0.05)
            assert "The receiver is not ready for a rescan." in _log_text(app)
            app.stop_timeout_s = 0.2
            await pilot.press("q")

        _drive(app, scenario)

    def test_paused_view_holds_updates_until_resumed(self, tmp_path):
        app = _dashboard(tmp_path)

        async def scenario(pilot):
            await _until(pilot, lambda: app.state.consensus is not None)
            await pilot.press("p")
            await pilot.press("r")
            await _until(pilot, lambda: app.state.iteration == 2)
            await pilot.pause(0.05)
            status = app.query_one("#status", Static)
            assert str(status.content).startswith("VIEW PAUSED")
            assert any(message.startswith("Acquisition #2") for message in app.held)
            await pilot.press("p")
            assert not app.held
            assert str(status.content).startswith("ACQUISITION #2")
            await pilot.press("q")

        _drive(app, scenario)

    def test_stream_ending_without_a_stop_is_a_failure(self, tmp_path):
        app = _dashboard(tmp_path, mode="truncated")
        assert app.held.maxlen == tui.LOG_LINES

        async def scenario(pilot):
            await _until(pilot, lambda: app.state.phase == "STOPPED")
            await pilot.pause(0.05)
            assert "The receiver stream ended without recon_stopped." in _log_text(app)
            await pilot.press("q")

        _drive(app, scenario)
        assert app.return_code == 1

    def test_receiver_failure_is_shown_and_returned(self, tmp_path):
        app = _dashboard(tmp_path, mode="fail")

        async def scenario(pilot):
            await _until(pilot, lambda: app.state.phase == "STOPPED")
            await pilot.pause(0.05)
            log = _log_text(app)
            assert "Receiver process exited with status 1." in log
            assert "Connection refused" in log
            await pilot.press("q")

        _drive(app, scenario)
        assert app.return_code == 1

    def test_receiver_that_ignores_sigint_is_killed(self, tmp_path):
        app = _dashboard(tmp_path, mode="ignore-sigint")
        app.stop_timeout_s = 0.3

        async def scenario(pilot):
            await _until(pilot, lambda: app.state.ready)
            await pilot.press("q")

        _drive(app, scenario)
        assert app.child.process.returncode == -signal.SIGKILL
        assert app.return_code == 0

    def test_missing_receiver_command_is_reported(self, tmp_path):
        app = tui.ReconDashboard([str(tmp_path / "missing-rdsclock")])

        async def scenario(pilot):
            await _until(pilot, lambda: app.state.phase == "STOPPED")
            assert "Could not start the receiver" in _log_text(app)
            await pilot.press("q")

        _drive(app, scenario)
        assert app.return_code == 1

    def test_any_exit_stops_the_receiver(self, tmp_path):
        app = _dashboard(tmp_path)

        async def scenario(pilot):
            await _until(pilot, lambda: app.state.ready)
            app.exit()

        _drive(app, scenario)
        assert app.child.process.returncode == 0

    @pytest.mark.parametrize(
        ("failed", "return_code", "status"), [(False, None, 0), (False, 3, 3), (True, 0, 1)]
    )
    def test_run_dashboard_exit_status(self, monkeypatch, failed, return_code, status):
        class FinishedApp:
            def __init__(self, command, language="en"):
                self.failed, self.return_code = failed, return_code

            def run(self):
                pass

        monkeypatch.setattr(tui, "ReconDashboard", FinishedApp)
        assert tui.run_dashboard(["rdsclock"]) == status


class TestReconTuiCli:
    def test_child_runs_jsonl_with_the_same_settings(self, monkeypatch):
        commands: list[list[str]] = []
        monkeypatch.setattr(
            tui, "run_dashboard", lambda command, language="en": commands.append(command) or 0
        )
        argv = [
            "recon",
            "--start=98",
            "--end=108",
            "--step=0.1",
            "--rssi-threshold=-35",
            "--gain=19.7",
            "--host=127.0.0.1",
            "--port=1235",
            "--iterations=3",
            "--settle-seconds=1",
        ]
        assert cli.main([*argv, "--tui"]) == 0
        (command,) = commands
        assert command[:5] == [sys.executable, "-m", "rdsclock", "recon", "--jsonl"]
        parser = cli.build_parser()
        child = cli._recon_config(parser.parse_args(command[3:]))
        assert child == cli._recon_config(parser.parse_args(argv))

    def test_child_receives_every_receiver_setting(self):
        dests = set(vars(cli.build_parser().parse_args(["recon"])))
        local = {
            "command",
            "func",
            "verbose",
            "jsonl",
            "tui",
            "web",
            "web_host",
            "web_port",
            "lang",
            "gui",
        }
        local |= {"from_dir", "limit_files"}
        assert {dest for _, dest in cli._RECON_CHILD_OPTIONS} == dests - local

    def test_offline_replay_is_rejected(self, tmp_path, capsys):
        assert cli.main(["recon", "--tui", "--from-dir", str(tmp_path)]) == 2
        assert "--from-dir" in capsys.readouterr().err

    def test_tui_and_jsonl_are_exclusive(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["recon", "--tui", "--jsonl"])
        assert exc.value.code == 2
        assert "not allowed with argument" in capsys.readouterr().err

    def test_missing_extra_is_explained(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None)
        assert cli.main(["recon", "--tui"]) == 2
        assert "pip install 'rdsclock[tui]'" in capsys.readouterr().err


@pytest.mark.parametrize(
    "language,trust,rescan,paused",
    [
        ("en", "HIGH", "Rescan", "VIEW PAUSED"),
        ("pl", "WYSOKIE", "Skanuj ponownie", "WIDOK WSTRZYMANY"),
        ("fr", "ÉLEVÉE", "Relancer le balayage", "AFFICHAGE SUSPENDU"),
    ],
)
def test_dashboard_language_reaches_widgets_and_preserves_style(
    tmp_path, language, trust, rescan, paused
):
    app = tui.ReconDashboard(receiver_command(tmp_path), language=language)

    async def scenario(pilot):
        await _until(pilot, lambda: app.state.consensus is not None)
        panel = str(app.query_one("#consensus", Static).render())
        assert trust in panel
        assert app._bindings.key_to_bindings["r"][0].description == rescan
        cells = tui.row_cells(app.state.view().rows[2], language)
        assert cells[1].plain == MARKUP_PS
        assert cells[-1].style == "bold red"
        await pilot.press("p")
        assert paused in str(app.query_one("#status", Static).render())
        await pilot.press("q")

    _drive(app, scenario)
    assert app.child.process.returncode == 0


@pytest.mark.parametrize(
    "language,ambiguous", [("en", "ambiguous, not used"), ("pl", "niejednoznaczny, nieużyty")]
)
def test_station_table_shows_ct_read_as_local_time(tmp_path, language, ambiguous):
    app = tui.ReconDashboard(receiver_command(tmp_path, records=ct_session()), language=language)

    async def scenario(pilot):
        await _until(pilot, lambda: app.state.consensus is not None)
        app._refresh()
        table = app.query_one(DataTable)
        assert str(table.get_row_at(0)[6]) == "12:00Z ← Europe/Warsaw"
        assert str(table.get_row_at(2)[6]).endswith(ambiguous)
        assert "CT 13:00 " in _log_text(app) and "= 12:00Z (LTO +00:00)" in _log_text(app)
        await pilot.press("q")

    _drive(app, scenario)
