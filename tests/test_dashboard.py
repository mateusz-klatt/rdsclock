"""Dashboard model shared by the terminal, web and desktop front ends."""

import io
import json
import signal
import threading
import time
from datetime import timedelta

import pytest

import rdsclock.dashboard as dashboard
import rdsclock.recon as recon
from rdsclock.rds_clock import ClockTime
from tests.fake_receiver import (
    CT,
    HOST_UTC,
    MARKUP_PS,
    STATION_A,
    cycle,
    line,
    receiver_command,
    session,
)


class _Clock:
    """The dashboard's monotonic clock and a host clock that follows it."""

    def __init__(self) -> None:
        self.now = 100.0
        self.host_step = timedelta(0)

    def __call__(self) -> float:
        return self.now

    def wall(self):
        return HOST_UTC + timedelta(seconds=self.now - 100.0) + self.host_step


def _state(lines: list[str]) -> tuple[dashboard.DashboardState, _Clock, list[str]]:
    clock = _Clock()
    state = dashboard.DashboardState(clock=clock, wall_clock=clock.wall)
    messages = [message for record in lines for message in state.apply_line(record)]
    return state, clock, messages


def _hop(
    pi: int | None, ct: ClockTime | None, freq_hz: float = 102.4e6, ps: str = "STACJA A"
) -> str:
    return line(recon.HopResult(freq_hz, pi, ps, -18.2, 812, 795, 17, ct))


def _cells(row: dashboard.Row) -> list[str]:
    return [*row.cells, row.status]


class TestDashboardState:
    def test_session_fills_the_station_table(self):
        state, _, messages = _state(session())
        rows = {cells[0]: cells for cells in map(_cells, state.view().rows)}
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
        assert len(dashboard.COLUMNS) == len(rows["102.40"])
        assert "Receiver ready: tuner type 5, 29 gain steps." in messages
        assert "Watchlist: 102.40, 98.80, 107.50 MHz." in messages

    def test_dashboard_clock_advances_time_ages_and_countdown(self):
        state, clock, _ = _state(session())
        clock.now += 5
        view = state.view()
        assert view.consensus == dashboard.ConsensusView(
            trust_level="HIGH",
            utc="2026-11-03 12:00:35",
            uncertainty="±3 s",
            sources="N=2",
            age="age 46 s",
            host_offset="Host clock minus consensus: +10.0 s",
            outliers="Outliers: 107.50 MHz",
            notes="",
            assessed="assessed 5 s ago",
        )
        clock.host_step = timedelta(seconds=60)  # e.g. an NTP correction between cycles
        assert state.view().consensus.host_offset == "Host clock minus consensus: +70.0 s"
        assert view.rows[0].cells[8] == "46 s"
        assert view.status.endswith("next rescan in 407 s")

    def test_status_line_follows_scan_and_hops(self):
        lines = session()
        state, _, _ = _state(lines[:3])
        assert state.view().status == "ACQUISITION #1 · 98.80 MHz · 1/101 points"
        state, _, _ = _state(lines[:7])
        assert state.view().status == "MAINTENANCE #1 · hop 2/3 · recording 98.80 MHz"
        state, _, _ = _state([*lines[:5], line(recon.MaintenanceStarted(1, 0))])
        assert state.view().status == "MAINTENANCE #1 · no stations to visit"

    def test_unavailable_time_is_explicit(self):
        state, _, _ = _state([*session()[:-1], line(cycle(utc=None))])
        consensus = state.view().consensus
        assert consensus.utc is None
        assert (consensus.trust_level, consensus.notes) == ("STALE", "no active sources")
        assert (consensus.age, consensus.host_offset, consensus.outliers) == (None, None, None)
        assert consensus.uncertainty == "±? s"
        assert dashboard.DashboardState().view().consensus is None

    def test_view_is_plain_json(self):
        state, _, _ = _state(session())
        decoded = json.loads(json.dumps(state.view().as_dict()))
        assert decoded["phase"] == "MAINTENANCE"
        assert decoded["rows"][2] == {"cells": decoded["rows"][2]["cells"], "status": "OUTLIER"}
        assert decoded["consensus"]["trust_level"] == "HIGH"

    def test_rescan_is_possible_only_while_the_receiver_runs(self):
        state, _, _ = _state([])
        assert not state.can_rescan()
        state.apply_line(session()[0])
        assert state.can_rescan()
        state.receiver_exited(1, [], requested=False)
        assert not state.can_rescan()

    def test_unexpected_input_is_reported_and_not_applied(self):
        state, _, _ = _state(session())
        assert state.apply_line("not json") == ["Ignored a line that is not JSON."]
        assert state.apply_line("[1, 2]") == ["Ignored a JSON value that is not an event."]
        assert state.apply_line(json.dumps({"event": "future_event", "schema": 1})) == []
        hop = json.loads(session()[6])
        hop["n_groups"] = "812"
        before = dict(state.hops)
        assert state.apply_line(json.dumps(hop)) == ["Ignored a malformed hop_result event."]
        assert state.hops == before
        scan = json.loads(session()[2])
        scan["freq_hz"] = "98.8"
        assert state.apply_line(json.dumps(scan)) == ["Ignored a malformed scan_step event."]
        late = json.loads(session()[-1])
        late["consensus"]["utc"] = "2026-11-03T12:00:30"  # no UTC offset
        assert state.apply_line(json.dumps(late)) == ["Ignored a malformed cycle_completed event."]
        for freq in ("Infinity", "1" + "0" * 400):  # not finite; beyond the float range
            record = session()[6].replace('"freq_hz":102400000.0', f'"freq_hz":{freq}')
            assert state.apply_line(record) == ["Ignored a malformed hop_result event."]
        newer = {"event": "recon_stopped", "schema": 2, "iteration": 1, "reason": "interrupted"}
        messages = state.apply_line(json.dumps(newer))
        assert messages[0].startswith("Event schema 2 is not 1")
        assert messages[1] == "Receiver stopped: stopped on request."
        assert state.apply_line(json.dumps(newer)) == ["Receiver stopped: stopped on request."]

    def test_empty_watchlist_and_uncounted_tracks(self):
        _, _, messages = _state([line(recon.WatchlistUpdated(1, ()))])
        assert messages == ["Watchlist empty: no RDS station found in this scan."]
        late = json.loads(session()[-1])
        late["consensus"]["contributing_freqs_mhz"] = [102.4]
        state, _, _ = _state([*session()[:-1], json.dumps(late)])
        statuses = {row.cells[0]: row.status for row in state.view().rows}
        assert statuses == {"102.40": "ok", "98.80": "not counted", "107.50": "OUTLIER"}

    def test_estimate_expires_with_the_consensus_freshness_limit(self):
        state, clock, _ = _state(session())
        clock.now += 259  # median CT age 41 s + 259 s = 300 s: still active
        assert state.view().consensus.trust_level == "HIGH"
        clock.now += 1
        expired = state.view().consensus
        assert (expired.utc, expired.trust_level, expired.outliers) == (None, "STALE", None)
        assert expired.notes == "the last estimate expired: its CT is 301 s old"
        assert expired.assessed == "assessed 260 s ago"

    def test_tunings_in_one_100_khz_bin_stay_apart(self):
        near = recon.StationCandidate(102.42e6, -30.0, 90, True, 0xB202, "NEAR")
        late = json.loads(line(cycle()))
        late["watchlist"] = [
            late["watchlist"][0],
            json.loads(line(recon.ScanStep(0, 0, near)))["candidate"],
        ]
        state, _, _ = _state(
            [
                *session()[:6],
                _hop(0xA101, ClockTime(CT, 60)),
                _hop(0xB202, None, freq_hz=102.42e6),
                json.dumps(late),
            ]
        )
        rows = {row.cells[0]: row for row in state.view().rows}
        assert (rows["102.40"].cells[2], rows["102.40"].cells[6], rows["102.40"].status) == (
            "0xA101",
            "12:00Z",
            "ok",
        )
        assert (rows["102.42"].cells[2], rows["102.42"].cells[6], rows["102.42"].status) == (
            "0xB202",
            "none",
            "no CT",
        )

    def test_the_freshest_track_of_a_tuning_is_shown(self):
        late = json.loads(line(cycle()))
        first, second, third = late["tracks"]
        late["tracks"] = [
            dict(first, trust_score=0.9, age_s=200.0),
            dict(first, trust_score=0.1, age_s=10.0),
            dict(second, trust_score=0.3, age_s=None),
            dict(second, trust_score=0.2, age_s=None),
            dict(third, trust_score=0.4, age_s=50.0),
            dict(third, trust_score=0.5, age_s=None),
        ]
        state, _, _ = _state([*session()[:-1], json.dumps(late)])
        ages_and_trust = [row.cells[8:] for row in state.view().rows]
        assert ages_and_trust == [("10 s", "0.10"), ("—", "0.20"), ("50 s", "0.40")]

    def test_unconfirmed_hop_pi_is_not_borrowed_from_the_scan(self):
        state, _, _ = _state([*session()[:6], _hop(None, ClockTime(CT, 60))])
        assert STATION_A.pi == 0xA101
        assert state.view().rows[0].cells[2] == "—"

    def test_last_ct_survives_hops_without_ct_until_the_pi_changes(self):
        lines = [*session()[:6], _hop(0xA101, ClockTime(CT, 60)), _hop(0xA101, None)]
        state, _, _ = _state(lines)
        assert state.view().rows[0].cells[6:8] == ("12:00Z", "+01:00")
        state.apply_line(_hop(None, None))  # an unconfirmed PI cannot prove a change
        assert state.view().rows[0].cells[6] == "12:00Z"
        state.apply_line(_hop(0xBEEF, None))
        assert state.view().rows[0].cells[6:8] == ("none", "")
        assert state.view().rows[0].status == "no CT"

    def test_an_unconfirmed_ct_is_not_shown_beside_another_transmitter(self):
        unconfirmed = ClockTime(CT.replace(minute=5), 60)
        lines = [*session()[:6], _hop(0xA101, ClockTime(CT, 60)), _hop(None, unconfirmed)]
        state, _, _ = _state([*lines, _hop(0xB202, None)])
        assert state.view().rows[0].cells[2] == "0xB202"
        assert state.view().rows[0].cells[6] == "none"

    def test_a_rescan_that_confirms_another_pi_drops_the_tuning_caches(self):
        moved = recon.StationCandidate(102.4e6, -30.0, 90, True, 0xB202, "OTHER")
        state, _, _ = _state([*session(), line(recon.WatchlistUpdated(2, (moved,)))])
        assert state.view().rows[0].cells[1:3] == ("OTHER", "0xB202")
        assert state.view().rows[0].cells[5:8] == ("", "", "")

    def test_metrics_of_another_transmitter_are_not_shown(self):
        state, _, _ = _state([*session(), _hop(0xBEEF, None)])
        row = state.view().rows[0]
        assert (row.cells[2], row.cells[8:], row.status) == ("0xBEEF", ("—", "—"), "no CT")

    def test_scan_name_is_reused_only_for_the_same_confirmed_pi(self):
        lines = session()[:6]
        state, _, _ = _state([*lines, _hop(0xA101, None, ps="")])
        assert state.view().rows[0].cells[1] == "STACJA A"
        state, _, _ = _state([*lines, _hop(0xB202, None, ps="")])
        assert state.view().rows[0].cells[1] == ""
        state, _, _ = _state([*lines, _hop(None, None, ps="")])
        assert state.view().rows[0].cells[1] == ""

    def test_a_stream_that_ends_without_a_stop_has_failed(self):
        state, _, _ = _state(session())
        assert state.exit_failed(0, requested=False)
        assert not state.exit_failed(1, requested=True)
        assert state.receiver_exited(0, [], requested=False) == [
            "The receiver stream ended without recon_stopped."
        ]
        state.apply_line(
            json.dumps({"event": "recon_stopped", "schema": 1, "reason": "iterations"})
        )
        assert not state.exit_failed(0, requested=False)
        assert state.exit_failed(1, requested=False)

    def test_broadcast_text_is_reduced_to_printable_characters(self):
        hostile = recon.StationCandidate(95.5e6, -20.0, 5, False, None, "\x1b[2J[b]X[/b]")
        state, _, messages = _state([line(recon.WatchlistUpdated(1, (hostile,)))])
        assert state.view().rows[0].cells[1] == "\ufffd[2J[b]X[/b]"
        assert all(text.isprintable() for text in messages)

    def test_receiver_exit_is_quiet_only_after_a_clean_stop(self):
        state, _, _ = _state(session())
        assert state.receiver_exited(0, ["status block"], requested=True) == []
        assert state.receiver_exited(1, ["refused\x07"], requested=False) == [
            "Receiver process exited with status 1.",
            "  refused\ufffd",
        ]
        assert state.phase == "STOPPED"
        assert state.start_failed(FileNotFoundError("missing")) == [
            "Could not start the receiver: missing"
        ]


class TestBoundedLines:
    def test_overlong_lines_become_empty_and_the_rest_survives(self):
        stream = io.BytesIO(b"first\n" + b"x" * 50 + b"\nlast")
        assert list(dashboard.bounded_lines(stream, limit=16)) == ["first\n", "", "last"]

    def test_invalid_utf8_is_replaced(self):
        stream = io.BytesIO(b"\xffok\n")
        assert list(dashboard.bounded_lines(stream)) == ["\ufffdok\n"]


class _Collector:
    """Callbacks for ReceiverProcess, collected from its reader threads."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.exits: list[tuple[int, list[str]]] = []
        self._lock = threading.Lock()

    def on_line(self, text: str) -> None:
        with self._lock:
            self.lines.append(text)

    def on_exit(self, returncode: int, stderr_tail: list[str]) -> None:
        with self._lock:
            self.exits.append((returncode, stderr_tail))

    def wait_for(self, condition, timeout_s: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_s
        while not condition():
            assert time.monotonic() < deadline, "the receiver did not reach the expected state"
            time.sleep(0.02)


def _receiver(tmp_path, mode: str = "follow") -> tuple[dashboard.ReceiverProcess, _Collector]:
    collector = _Collector()
    receiver = dashboard.ReceiverProcess(
        receiver_command(tmp_path, mode), collector.on_line, collector.on_exit
    )
    return receiver, collector


class TestReceiverProcess:
    def test_lines_rescan_and_clean_stop(self, tmp_path):
        receiver, collector = _receiver(tmp_path)
        receiver.start()
        collector.wait_for(lambda: len(collector.lines) == len(session()))
        assert receiver.send_signal(signal.SIGUSR1)
        collector.wait_for(lambda: len(collector.lines) == len(session()) + 1)
        assert json.loads(collector.lines[-1])["event"] == "acquisition_started"
        assert receiver.stop(timeout_s=5.0)
        receiver.join()
        assert collector.exits == [(0, [])]
        assert json.loads(collector.lines[-1])["event"] == "recon_stopped"
        assert not receiver.send_signal(signal.SIGUSR1)
        assert receiver.stop(timeout_s=0.1)

    def test_failure_reports_the_stderr_tail(self, tmp_path):
        receiver, collector = _receiver(tmp_path, mode="fail")
        receiver.start()
        receiver.join(timeout_s=15.0)
        assert collector.exits == [(1, ["rdsclock: [Errno 111] Connection refused"])]

    def test_receiver_that_ignores_sigint_is_killed(self, tmp_path):
        receiver, collector = _receiver(tmp_path, mode="ignore-sigint")
        receiver.start()
        collector.wait_for(lambda: len(collector.lines) == len(session()))
        assert not receiver.stop(timeout_s=0.3)
        receiver.join(timeout_s=15.0)
        assert collector.exits == [(-signal.SIGKILL, [])]

    def test_overlong_line_is_dropped(self, tmp_path):
        receiver, collector = _receiver(tmp_path, mode="overlong")
        receiver.start()
        collector.wait_for(lambda: len(collector.lines) == len(session()) + 1)
        assert collector.lines[0] == ""
        receiver.stop(timeout_s=5.0)
        receiver.join(timeout_s=15.0)

    def test_signal_to_a_vanished_child_is_not_sent(self, tmp_path):
        class Vanished:
            def poll(self):
                return None

            def send_signal(self, signum):
                raise ProcessLookupError

        receiver, _ = _receiver(tmp_path)
        receiver.process = Vanished()
        assert not receiver.send_signal(signal.SIGUSR1)
        assert receiver.stop(timeout_s=0.1)

    def test_missing_command_raises(self, tmp_path):
        collector = _Collector()
        receiver = dashboard.ReceiverProcess(
            [str(tmp_path / "missing-rdsclock")], collector.on_line, collector.on_exit
        )
        with pytest.raises(OSError):
            receiver.start()
        assert not receiver.running
