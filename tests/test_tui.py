"""Terminal dashboard (``recon --tui``): state model, Textual app and CLI wiring."""

import asyncio
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, RichLog, Static  # noqa: E402

import rdsclock.cli as cli  # noqa: E402
import rdsclock.recon as recon  # noqa: E402
import rdsclock.tui as tui  # noqa: E402
from rdsclock.rds_clock import ClockTime  # noqa: E402
from rdsclock.time_consensus import ConsensusResult, TrustLevel  # noqa: E402

HOST_UTC = datetime(2026, 11, 3, 12, 0, 40, tzinfo=UTC)
CT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
MARKUP_PS = "[red]C[/]"

STATION_A = recon.StationCandidate(102.4e6, -18.2, 812, True, 0xA101, "STACJA A")
STATION_B = recon.StationCandidate(98.8e6, -21.5, 643, True, 0xA102, "STACJA B")
STATION_C = recon.StationCandidate(107.5e6, -24.0, 840, True, 0xA103, MARKUP_PS)


def _line(event: recon.ReconEvent) -> str:
    return recon.event_to_json(event, host_utc=HOST_UTC)


def _cycle(utc: datetime | None = CT + timedelta(seconds=30)) -> recon.CycleCompleted:
    consensus = ConsensusResult(
        utc=utc,
        uncertainty_s=3.0 if utc else float("inf"),
        trust_level=TrustLevel.HIGH if utc else TrustLevel.STALE,
        n_sources=2 if utc else 0,
        contributing_freqs_mhz=[98.8, 102.4] if utc else [],
        outlier_freqs_mhz=[107.5] if utc else [],
        median_age_s=41.0 if utc else float("inf"),
        notes="" if utc else "no active sources",
    )
    tracks = (
        recon.TrackSnapshot(102.4e6, 0xA101, 2, 0.6, 0, 41.0, CT),
        recon.TrackSnapshot(98.8e6, 0xA102, 2, 0.6, 0, 63.0, CT),
        recon.TrackSnapshot(107.5e6, 0xA103, 2, 0.1, 2, 80.0, CT),
    )
    return recon.CycleCompleted(1, 3, consensus, (STATION_A, STATION_B, STATION_C), tracks, 412.0)


def _session() -> list[str]:
    """One acquisition and one maintenance cycle, as ``recon --jsonl`` writes them."""
    return [
        _line(recon.ReconStarted(tuner_type=5, gain_count=29)),
        _line(recon.AcquisitionStarted(1, 98.0, 108.0, 0.1)),
        _line(recon.ScanStep(98.8e6, -21.5, STATION_B)),
        _line(recon.ScanStep(98.9e6, -60.0, None)),
        _line(recon.WatchlistUpdated(1, (STATION_A, STATION_B, STATION_C))),
        _line(recon.MaintenanceStarted(1, 3)),
        _line(recon.HopResult(102.4e6, 0xA101, "STACJA A", -18.2, 812, 795, 17, ClockTime(CT, 60))),
        _line(recon.HopResult(98.8e6, 0xA102, "STACJA B", -21.5, 643, 640, 3, None)),
        _line(
            recon.HopResult(
                107.5e6, 0xA103, MARKUP_PS, -24.0, 840, 838, 2, ClockTime(CT.replace(minute=10), 60)
            )
        ),
        _line(_cycle()),
    ]


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _state(lines: list[str]) -> tuple[tui.DashboardState, _Clock, list[str]]:
    clock = _Clock()
    state = tui.DashboardState(clock=clock)
    messages = [message for line in lines for message in state.apply_line(line)]
    return state, clock, messages


def _cells(row: tuple) -> list[str]:
    return [cell.plain for cell in row]


class TestDashboardState:
    def test_session_fills_the_station_table(self):
        state, _, messages = _state(_session())
        rows = {cells[0]: cells for cells in map(_cells, state.station_rows())}
        assert list(rows) == ["102.40", "98.80", "107.50"]
        assert rows["102.40"] == [
            "102.40",
            "STACJA A",
            "0xA101",
            "-18.2",
            "812",
            "17",
            "12:00Z",
            "+01:00",
            "41 s",
            "0.60",
            "ok",
        ]
        assert rows["98.80"][6:] == ["none", "", "63 s", "0.60", "ok"]
        assert rows["107.50"][1] == MARKUP_PS
        assert rows["107.50"][-1] == "OUTLIER"
        assert "Receiver ready: tuner type 5, 29 gain steps." in messages
        assert "Watchlist: 102.40, 98.80, 107.50 MHz." in messages

    def test_dashboard_clock_advances_time_ages_and_countdown(self):
        state, clock, _ = _state(_session())
        clock.now += 5
        text = state.consensus_text().plain
        assert "UTC 2026-11-03 12:00:35   ±3 s   HIGH   N=2   age 46 s" in text
        assert "Host clock minus consensus: +10.0 s" in text
        assert "Outliers: 107.50 MHz" in text
        assert "not a calibrated UTC error" in text
        assert _cells(state.station_rows()[0])[8] == "46 s"
        assert state.status_line().plain.endswith("next rescan in 407 s")

    def test_status_line_follows_scan_and_hops(self):
        lines = _session()
        state, _, _ = _state(lines[:3])
        assert state.status_line().plain == "ACQUISITION #1 · 98.80 MHz · 1/101 points"
        state, _, _ = _state(lines[:7])
        assert state.status_line().plain == "MAINTENANCE #1 · hop 2/3 · recording 98.80 MHz"
        state, _, _ = _state([*lines[:5], _line(recon.MaintenanceStarted(1, 0))])
        assert state.status_line().plain == "MAINTENANCE #1 · no stations to visit"

    def test_unavailable_time_is_explicit(self):
        state, _, _ = _state([*_session()[:-1], _line(_cycle(utc=None))])
        text = state.consensus_text().plain
        assert text.startswith("TIME UNAVAILABLE   trust STALE   no active sources")
        assert "Waiting" in tui.DashboardState().consensus_text().plain

    def test_unexpected_input_is_reported_and_not_applied(self):
        state, _, _ = _state(_session())
        assert state.apply_line("not json") == ["Ignored a line that is not JSON."]
        assert state.apply_line("[1, 2]") == ["Ignored a JSON value that is not an event."]
        assert state.apply_line(json.dumps({"event": "future_event", "schema": 1})) == []
        hop = json.loads(_session()[6])
        hop["n_groups"] = "812"
        before = dict(state.hops)
        assert state.apply_line(json.dumps(hop)) == ["Ignored a malformed hop_result event."]
        assert state.hops == before
        scan = json.loads(_session()[2])
        scan["freq_hz"] = "98.8"
        assert state.apply_line(json.dumps(scan)) == ["Ignored a malformed scan_step event."]
        cycle = json.loads(_session()[-1])
        cycle["host_utc"] = "2026-11-03T12:00:40"  # no UTC offset
        assert state.apply_line(json.dumps(cycle)) == ["Ignored a malformed cycle_completed event."]
        for freq in ("Infinity", "1" + "0" * 400):  # not finite; beyond the float range
            line = _session()[6].replace('"freq_hz":102400000.0', f'"freq_hz":{freq}')
            assert state.apply_line(line) == ["Ignored a malformed hop_result event."]
        newer = {"event": "recon_stopped", "schema": 2, "iteration": 1, "reason": "interrupted"}
        messages = state.apply_line(json.dumps(newer))
        assert messages[0].startswith("Event schema 2 is not 1")
        assert messages[1] == "Receiver stopped: stopped on request."
        assert state.apply_line(json.dumps(newer)) == ["Receiver stopped: stopped on request."]

    def test_empty_watchlist_and_uncounted_tracks(self):
        _, _, messages = _state([_line(recon.WatchlistUpdated(1, ()))])
        assert messages == ["Watchlist empty: no RDS station found in this scan."]
        cycle = json.loads(_session()[-1])
        cycle["consensus"]["contributing_freqs_mhz"] = [102.4]
        state, _, _ = _state([*_session()[:-1], json.dumps(cycle)])
        statuses = {cells[0]: cells[-1] for cells in map(_cells, state.station_rows())}
        assert statuses == {"102.40": "ok", "98.80": "not counted", "107.50": "OUTLIER"}

    def test_broadcast_text_cannot_style_or_control_the_terminal(self):
        hostile = recon.StationCandidate(95.5e6, -20.0, 5, False, None, "\x1b[2J[b]X[/b]")
        state, _, messages = _state([_line(recon.WatchlistUpdated(1, (hostile,)))])
        ps_cell = state.station_rows()[0][1]
        assert ps_cell.plain == "\ufffd[2J[b]X[/b]"
        assert not ps_cell.spans
        assert all(text.isprintable() for text in messages)

    def test_receiver_exit_is_quiet_only_after_a_clean_stop(self):
        state, _, _ = _state(_session())
        assert state.receiver_exited(0, ["status block"], requested=True) == []
        assert state.receiver_exited(1, ["refused\x07"], requested=False) == [
            "Receiver process exited with status 1.",
            "  refused\ufffd",
        ]
        assert state.phase == "STOPPED"
        assert state.start_failed(FileNotFoundError("missing")) == [
            "Could not start the receiver: missing"
        ]


class _ClosedStream:
    async def readline(self) -> bytes:
        return b""


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


# A stand-in for ``rdsclock recon --jsonl``: replays a file, answers SIGUSR1
# with a new acquisition and SIGINT with recon_stopped, like the real loop.
_FAKE_RECEIVER = """
import json, signal, sys, time

path, mode = sys.argv[1], sys.argv[2]

def emit(record):
    print(json.dumps({"schema": 1, "host_utc": "2026-11-03T12:01:00+00:00", **record}), flush=True)

def rescan(signum, frame):
    emit({"event": "acquisition_started", "iteration": 2,
          "band_start_mhz": 98.0, "band_end_mhz": 108.0, "scan_step_mhz": 0.1})

signal.signal(signal.SIGUSR1, rescan)
if mode == "ignore-sigint":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
if mode == "overlong":
    print("x" * (2 << 20), flush=True)
for line in open(path, encoding="utf-8"):
    print(line, end="", flush=True)
if mode == "fail":
    print("rdsclock: [Errno 111] Connection refused", file=sys.stderr, flush=True)
    sys.exit(1)
try:
    while True:
        time.sleep(0.02)
except KeyboardInterrupt:
    emit({"event": "recon_stopped", "iteration": 1, "reason": "interrupted"})
"""


def _dashboard(tmp_path, mode: str = "follow") -> tui.ReconDashboard:
    session = tmp_path / "session.jsonl"
    session.write_text("\n".join(_session()) + "\n", encoding="utf-8")
    return tui.ReconDashboard([sys.executable, "-I", "-c", _FAKE_RECEIVER, str(session), mode])


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
            def __init__(self, command):
                self.failed, self.return_code = failed, return_code

            def run(self):
                pass

        monkeypatch.setattr(tui, "ReconDashboard", FinishedApp)
        assert tui.run_dashboard(["rdsclock"]) == status


class TestReconTuiCli:
    def test_child_runs_jsonl_with_the_same_settings(self, monkeypatch):
        commands: list[list[str]] = []
        monkeypatch.setattr(tui, "run_dashboard", lambda command: commands.append(command) or 0)
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
        local = {"command", "func", "verbose", "jsonl", "tui", "from_dir", "limit_files"}
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
