"""HOP with several receivers: parallel hops, named receivers, lost receivers."""

import contextlib
import json
import signal
import socket
import struct
import threading
import time
from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

import rdsclock.cli as cli
import rdsclock.recon as recon
from rdsclock.rds_clock import ClockTime
from rdsclock.rds_groups import StationInfo
from rdsclock.rtl_tcp import RtlTcpAddress
from rdsclock.time_consensus import StationObservation, TimeConsensus
from tests.conftest import _make_fake_rtl_tcp
from tests.fake_receiver import line
from tests.test_dashboard import _state
from tests.test_recon_events import _cfg, _clock, _decode_result

DWELL_S = 0.4
PI_BASE = 0x1000


def _server_bytes(index: int) -> bytes:
    """A constant IQ level per server, so that the fake decoder can tell them apart."""
    return bytes([148 + 20 * index, 128] * 256)


def _decode_by_server(iq, fs, **kwargs):
    """Every server is one transmitter: its PI follows from the IQ level."""
    level = float(np.mean(iq.real)) * 127.5 + 127.5
    info = StationInfo(pi=PI_BASE + round((level - 148) / 20))
    info.clock_times.append(_clock())
    return replace(_decode_result(None), info=info)


@contextlib.contextmanager
def _servers(n: int):
    """``n`` fake rtl_tcp servers; yields (address, stop) for each."""
    started = [_make_fake_rtl_tcp(_server_bytes(index)) for index in range(n)]
    try:
        yield [(RtlTcpAddress("127.0.0.1", port), stop) for port, stop, _ in started]
    finally:
        for _, stop, thread in started:
            stop.set()
            thread.join(timeout=1.0)


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


STATIONS = ((98.8e6, 102.4e6), (98.8e6, 104.0e6), (107.5e6, 91.0e6))


def _multi_cfg(addresses, **overrides) -> recon.ReconConfig:
    hop_stations = tuple(
        (address, freq_hz)
        for address, freqs in zip(addresses, STATIONS, strict=False)
        for freq_hz in freqs
    )
    settings = {"dwell_s": DWELL_S, "settle_s": 0.05, "iterations": 1}
    settings.update(overrides)
    return _cfg(hop_stations=hop_stations, **settings)


class _Recorder:
    """Collects events, their arrival times and the operator text."""

    def __init__(self, on_event=None):
        self.events: list[recon.ReconEvent] = []
        self.times: list[float] = []
        self.status: list[str] = []
        self.progress: list[str] = []
        self.on_event = on_event

    def event(self, event):
        self.events.append(event)
        self.times.append(time.monotonic())
        if self.on_event is not None:
            self.on_event(event)

    def run(self, cfg, **kwargs):
        recon.run_recon(
            cfg,
            on_status=self.status.append,
            on_progress=self.progress.append,
            on_event=self.event,
            **kwargs,
        )

    def of(self, kind):
        return [event for event in self.events if event.kind == kind]


@pytest.fixture
def decode_by_server(monkeypatch):
    monkeypatch.setattr(recon, "decode_iq", _decode_by_server)


@pytest.mark.usefixtures("decode_by_server")
class TestParallelHop:
    def test_three_receivers_hop_at_once_into_one_consensus(self):
        recorder = _Recorder()
        with _servers(3) as servers:
            addresses = [address for address, _ in servers]
            recorder.run(_multi_cfg(addresses))

        names = [str(address) for address in addresses]
        kinds = [event.kind for event in recorder.events]
        assert kinds[:2] == ["recon_started", "acquisition_started"]
        assert kinds[2:8] == ["scan_step"] * 6
        assert kinds[8:10] == ["watchlist_updated", "maintenance_started"]
        assert kinds[10:16] == ["hop_result"] * 6
        assert kinds[16:] == ["cycle_completed", "recon_stopped"]
        started, acquisition, *_ = recorder.events
        assert started.receivers == tuple(names)
        assert (acquisition.n_points, acquisition.band_start_mhz) == (6, 91.0)
        assert recorder.of("maintenance_started")[0].n_stations == 6

        watchlist = recorder.of("watchlist_updated")[0].watchlist
        assert sorted((s.receiver, s.freq_hz) for s in watchlist) == sorted(
            (name, freq_hz)
            for name, freqs in zip(names, STATIONS, strict=True)
            for freq_hz in freqs
        )
        hops = recorder.of("hop_result")
        assert sorted((hop.receiver, hop.pi) for hop in hops) == sorted(
            (name, PI_BASE + index) for index, name in enumerate(names) for _ in range(2)
        )
        cycle = recorder.of("cycle_completed")[0]
        assert cycle.new_observations == 6
        assert cycle.consensus.n_sources == 3
        assert cycle.consensus.utc is not None

        # Every receiver records two stations at the same time as the others:
        # the pass takes about two dwells, far less than the six of one receiver.
        maintenance = recorder.times[kinds.index("maintenance_started")]
        elapsed = recorder.times[kinds.index("cycle_completed")] - maintenance
        assert 2 * DWELL_S <= elapsed < 4.5 * DWELL_S
        assert any(f"[{names[2]}] hop" in message for message in recorder.progress)
        assert "\n[#1] MAINTENANCE (hop 6 stations on 3 receivers)…" in recorder.status

    def test_events_stay_whole_json_lines_with_the_receivers(self):
        lines: list[str] = []
        with _servers(2) as servers:
            addresses = [address for address, _ in servers]
            recon.run_recon(
                _multi_cfg(addresses),
                on_status=lambda message: None,
                on_event=lambda event: lines.append(recon.event_to_json(event)),
            )

        records = [json.loads(text) for text in lines]
        names = [str(address) for address in addresses]
        assert records[0]["receivers"] == names
        candidates = next(r for r in records if r["event"] == "watchlist_updated")["watchlist"]
        assert {candidate["receiver"] for candidate in candidates} == set(names)
        hops = [record for record in records if record["event"] == "hop_result"]
        assert len(hops) == 4
        assert {hop["receiver"] for hop in hops} == set(names)

        state, _, _ = _state(lines)
        assert state.receivers == tuple(names)
        assert state.multi_receiver
        assert {key[0] for key in state.hops} == set(names)
        # Both receivers listed 98.8 MHz: two rows, one per receiver.
        assert len([s for s in state.watchlist if s.freq_hz == 98.8e6]) == 2
        assert len(state.view().rows) == 4

    def test_a_rescan_request_reaches_every_receiver(self):
        requests = iter([False, True])
        recorder = _Recorder()
        with _servers(2) as servers:
            addresses = [address for address, _ in servers]
            recorder.run(
                _multi_cfg(addresses, iterations=2), rescan_requested=lambda: next(requests)
            )
        assert [event.iteration for event in recorder.of("acquisition_started")] == [1, 2]
        assert [event.n_points for event in recorder.of("acquisition_started")] == [4, 4]
        assert len(recorder.of("hop_result")) == 8

    def test_one_named_receiver_runs_as_before(self):
        recorder = _Recorder()
        with _servers(1) as servers:
            address = servers[0][0]
            cfg = _cfg(
                hop_stations=((address, 98.8e6), (address, 102.4e6)),
                dwell_s=0.1,
                settle_s=0.05,
                iterations=1,
                host="127.0.0.1",
                port=_closed_port(),  # unused: the stations name their receiver
            )
            recorder.run(cfg)

        assert recorder.of("recon_started")[0].receivers == (str(address),)
        assert {hop.receiver for hop in recorder.of("hop_result")} == {str(address)}
        assert "\n[#1] MAINTENANCE (hop 2 stations)…" in recorder.status
        assert not recorder.of("receiver_failed")

    def test_the_default_receiver_is_named_without_a_receiver_list(self, rtl_tcp_factory):
        recorder = _Recorder()
        with rtl_tcp_factory(_server_bytes(0)) as port:
            cfg = _cfg(
                hop_freqs_hz=(98.8e6,),
                dwell_s=0.1,
                settle_s=0.05,
                iterations=1,
                host="127.0.0.1",
                port=port,
            )
            recorder.run(cfg)
        name = f"127.0.0.1:{port}"
        assert recorder.of("recon_started")[0].receivers == (name,)
        assert [hop.receiver for hop in recorder.of("hop_result")] == [name]


@pytest.mark.usefixtures("decode_by_server")
class TestLostReceivers:
    def test_the_others_go_on_while_a_lost_receiver_is_retried(self):
        stops: list[threading.Event] = []

        def stop_the_second_after_the_first_cycle(event):
            if event.kind == "cycle_completed" and event.iteration == 1:
                stops[1].set()

        recorder = _Recorder(stop_the_second_after_the_first_cycle)
        with _servers(3) as servers:
            addresses = [address for address, _ in servers]
            stops.extend(stop for _, stop in servers)
            recorder.run(_multi_cfg(addresses, iterations=3))

        names = [str(address) for address in addresses]
        failed = recorder.of("receiver_failed")
        # Lost during the second cycle, then refused when retried in the third.
        assert [(event.iteration, event.receiver) for event in failed] == [
            (2, names[1]),
            (3, names[1]),
        ]
        assert all(event.error for event in failed)
        assert recorder.events[-1] == recon.ReconStopped(iteration=3, reason="iterations")
        cycles = recorder.of("cycle_completed")
        assert [cycle.iteration for cycle in cycles] == [1, 2, 3]
        assert {s.receiver for s in cycles[0].watchlist} == set(names)
        assert {s.receiver for s in cycles[2].watchlist} == {names[0], names[2]}
        hops = recorder.of("hop_result")
        for name in (names[0], names[2]):
            assert sum(hop.receiver == name for hop in hops) == 6
        assert sum(hop.receiver == names[1] for hop in hops) < 4
        assert any(f"receiver {names[1]} failed" in message for message in recorder.status)

        lines = [line(event) for event in recorder.events]
        state, _, _ = _state(lines)
        assert names[1] in state.failed_receivers
        assert state.stopped

    def test_a_receiver_that_is_back_checks_its_stations_again(self):
        stops: list[threading.Event] = []
        restarted: list[tuple] = []
        addresses: list[RtlTcpAddress] = []

        def lose_then_restore(event):
            if event.kind == "cycle_completed" and event.iteration == 1:
                stops[1].set()
            if event.kind == "receiver_failed":
                # A new server at the same port: the receiver is back.
                port = addresses[1].port
                restarted.append(_restart(port))

        recorder = _Recorder(lose_then_restore)
        with _servers(2) as servers:
            addresses.extend(address for address, _ in servers)
            stops.extend(stop for _, stop in servers)
            try:
                recorder.run(_multi_cfg(addresses, iterations=3))
            finally:
                for stop, thread in restarted:
                    stop.set()
                    thread.join(timeout=1.0)

        names = [str(address) for address in addresses]
        assert [event.iteration for event in recorder.of("receiver_failed")] == [2]
        acquisitions = recorder.of("acquisition_started")
        assert [(a.iteration, a.n_points) for a in acquisitions] == [(1, 4), (3, 2)]
        assert any(f"receiver {names[1]} connected again" in m for m in recorder.status)
        watchlist = recorder.of("cycle_completed")[-1].watchlist
        assert {s.receiver for s in watchlist} == set(names)

    def test_a_receiver_refused_at_the_start_is_reported_and_the_others_run(self):
        recorder = _Recorder()
        dead = RtlTcpAddress("127.0.0.1", _closed_port())
        with _servers(2) as servers:
            addresses = [servers[0][0], dead, servers[1][0]]
            recorder.run(_multi_cfg(addresses))

        started = recorder.events[0]
        assert started.kind == "recon_started"
        assert started.receivers == tuple(str(address) for address in addresses)
        failed = recorder.of("receiver_failed")
        assert [(event.iteration, event.receiver) for event in failed] == [
            (0, str(dead)),
            (1, str(dead)),
        ]
        assert recorder.events[1] == failed[0]
        assert {hop.receiver for hop in recorder.of("hop_result")} == {
            str(addresses[0]),
            str(addresses[2]),
        }
        assert recorder.of("cycle_completed")[0].consensus.n_sources == 2

    def test_without_any_receiver_the_run_ends_with_the_error(self):
        recorder = _Recorder()
        addresses = [RtlTcpAddress("127.0.0.1", _closed_port()) for _ in range(2)]
        with pytest.raises(ConnectionRefusedError):
            recorder.run(_multi_cfg(addresses))
        assert recorder.events == []

    def test_when_every_receiver_is_lost_the_run_ends_with_the_error(self):
        stops: list[threading.Event] = []

        def stop_all(event):
            if event.kind == "cycle_completed":
                for stop in stops:
                    stop.set()

        recorder = _Recorder(stop_all)
        with _servers(2) as servers:
            addresses = [address for address, _ in servers]
            stops.extend(stop for _, stop in servers)
            with pytest.raises(OSError):
                recorder.run(_multi_cfg(addresses, iterations=5))
        assert len(recorder.of("receiver_failed")) == 2
        assert not recorder.of("recon_stopped")

    def test_a_failing_output_is_not_taken_for_a_lost_receiver(self):
        def refuse_hops(event):
            if event.kind == "hop_result":
                raise RuntimeError("disk full")

        recorder = _Recorder(refuse_hops)
        with _servers(2) as servers, pytest.raises(RuntimeError, match="disk full"):
            recorder.run(_multi_cfg([address for address, _ in servers]))
        assert not recorder.of("receiver_failed")

    def test_an_interruption_stops_every_receiver(self, monkeypatch):
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(recon, "hop_collect_ct", interrupt)
        recorder = _Recorder()
        with _servers(2) as servers:
            recorder.run(_multi_cfg([address for address, _ in servers]))
        assert recorder.events[-1] == recon.ReconStopped(iteration=1, reason="interrupted")
        assert "\nInterrupted by operator (Ctrl+C)." in recorder.status


class _OddServer:
    """A local rtl_tcp server for one connection that misbehaves.

    ``mode`` is ``"silent"`` (accepts, never sends the header), ``"stall"``
    (header, then nothing) or ``"trickle"`` (header, then one IQ sample every
    0.1 s, which no per-read timeout ever trips). ``tuned`` is set when the
    client first sets a frequency, ``closed`` when the client let go. It ends
    by itself after ``lifetime_s``, so that a regression fails, not hangs.
    """

    def __init__(self, mode: str, index: int = 0, lifetime_s: float = 20.0):
        self.mode = mode
        self.sample = _server_bytes(index)[:2]
        self.lifetime_s = lifetime_s
        self.accepted = threading.Event()
        self.tuned = threading.Event()
        self.closed = threading.Event()
        self.stop = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.address = RtlTcpAddress("127.0.0.1", self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self._thread.join(timeout=2.0)
        assert not self._thread.is_alive()

    def _serve(self) -> None:
        deadline = time.monotonic() + self.lifetime_s
        with self._listener:
            self._listener.settimeout(0.05)
            while True:
                if self.stop.is_set() or time.monotonic() > deadline:
                    return
                with contextlib.suppress(TimeoutError):
                    conn, _ = self._listener.accept()
                    break
        self.accepted.set()
        with conn:
            try:
                if self.mode != "silent":
                    conn.sendall(b"RTL0" + struct.pack(">II", 5, 29))
                self._talk(conn, deadline)
            except OSError:
                pass
            finally:
                self.closed.set()

    def _talk(self, conn: socket.socket, deadline: float) -> None:
        conn.settimeout(0.1)
        commands = bytearray()
        while not self.stop.is_set() and time.monotonic() < deadline:
            try:
                chunk = conn.recv(4096)
            except TimeoutError:
                if self.mode == "trickle":
                    conn.sendall(self.sample)
                continue
            if not chunk:
                return  # the client closed or shut down its end
            commands.extend(chunk)
            while len(commands) >= 5:
                if commands[0] == 1:
                    self.tuned.set()
                del commands[:5]
        # Out of time or told to stop: let the client see the end.


@contextlib.contextmanager
def _ctrl_c_when(*conditions: threading.Event, timeout_s: float = 10.0):
    """Press Ctrl+C in the main thread once every condition is set.

    Yields an event that is set when it was pressed. The run under test cannot
    end by itself before that, so the interrupt always lands inside it.
    """
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    pressed = threading.Event()

    def press() -> None:
        deadline = time.monotonic() + timeout_s
        for condition in conditions:
            if not condition.wait(max(0.0, deadline - time.monotonic())):
                return
        pressed.set()
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)

    presser = threading.Thread(target=press, daemon=True)
    presser.start()
    try:
        yield pressed
    finally:
        presser.join(timeout_s)
        signal.signal(signal.SIGINT, previous)


@pytest.fixture
def runs(monkeypatch):
    """Every :class:`recon._MultiReceiverRun` that a test makes."""
    made: list[recon._MultiReceiverRun] = []
    original = recon._MultiReceiverRun.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        made.append(self)

    monkeypatch.setattr(recon._MultiReceiverRun, "__init__", init)
    return made


def _assert_stopped_cleanly(run: "recon._MultiReceiverRun") -> None:
    assert run._stopping
    assert not any(thread.is_alive() for thread in run.threads)
    assert all(slot.client is None and slot.connecting is None for slot in run.slots)


@pytest.mark.usefixtures("decode_by_server")
class TestSlowAndStuckReceivers:
    def test_a_trickling_receiver_fails_its_cycle_and_the_others_go_on(self, monkeypatch):
        # Dwell 0.4 s needs 400 samples; at one per 0.1 s the read would take 40 s.
        monkeypatch.setattr(recon, "_CAPTURE_GRACE_MIN_S", 0.5)
        recorder = _Recorder()
        with _servers(1) as servers, _OddServer("trickle", index=1) as slow:
            addresses = [servers[0][0], slow.address]
            hop_stations = ((addresses[0], 102.4e6), (addresses[1], 104.0e6))
            started = time.monotonic()
            recorder.run(
                _cfg(hop_stations=hop_stations, dwell_s=DWELL_S, settle_s=0.05, iterations=1)
            )
            elapsed = time.monotonic() - started
            assert slow.closed.wait(2.0)

        failed = recorder.of("receiver_failed")
        assert [(event.iteration, event.receiver) for event in failed] == [(1, str(slow.address))]
        assert "before the capture deadline" in failed[0].error
        assert [hop.receiver for hop in recorder.of("hop_result")] == [str(addresses[0])]
        assert recorder.of("cycle_completed")[0].consensus.n_sources == 1
        assert recorder.events[-1] == recon.ReconStopped(iteration=1, reason="iterations")
        # The fake ends after 20 s: only the capture deadline can end it sooner.
        assert elapsed < 10.0

    def test_the_grace_is_at_least_15_s_and_a_quarter_of_the_dwell(self):
        assert recon._capture_grace_s(0.4) == 15.0
        assert recon._capture_grace_s(65.0) == 16.25
        assert recon._capture_grace_s(200.0) == 50.0

    def test_ctrl_c_while_waiting_for_a_stuck_receiver(self, runs):
        recorder = _Recorder()
        with _servers(1) as servers, _OddServer("stall", index=1) as stuck:
            addresses = [servers[0][0], stuck.address]
            with _ctrl_c_when(stuck.tuned) as pressed:
                started = time.monotonic()
                recorder.run(_multi_cfg(addresses, iterations=5))
                elapsed = time.monotonic() - started
            assert pressed.is_set()
            assert stuck.closed.wait(2.0)

        assert recorder.events[-1] == recon.ReconStopped(iteration=1, reason="interrupted")
        assert "\nInterrupted by operator (Ctrl+C)." in recorder.status
        _assert_stopped_cleanly(runs[0])
        # The stuck read would have lasted the 15 s read timeout.
        assert elapsed < 10.0

    def test_ctrl_c_during_a_handshake(self, runs):
        recorder = _Recorder()
        with _servers(1) as servers, _OddServer("silent") as mute:
            with _ctrl_c_when(mute.accepted) as pressed:
                started = time.monotonic()
                recorder.run(_multi_cfg([servers[0][0], mute.address]))
                elapsed = time.monotonic() - started
            assert pressed.is_set()
            assert mute.closed.wait(2.0)

        assert recorder.events == [recon.ReconStopped(iteration=0, reason="interrupted")]
        _assert_stopped_cleanly(runs[0])
        assert elapsed < 10.0  # the header read would have waited 15 s

    def test_ctrl_c_while_the_output_is_blocked(self, monkeypatch, runs):
        hops_done = threading.Event()
        output_blocked = threading.Event()
        release = threading.Event()
        finished: list[str] = []
        hop_collect_ct = recon.hop_collect_ct

        def hop_and_count(*args, **kwargs):
            try:
                return hop_collect_ct(*args, **kwargs)
            finally:
                finished.append(kwargs["receiver"])
                if len(finished) == 2:
                    hops_done.set()

        def block_at_the_first_hop(event):
            # A JSON Lines reader that stopped reading.
            if event.kind == "hop_result" and not output_blocked.is_set():
                output_blocked.set()
                release.wait(30.0)

        monkeypatch.setattr(recon, "hop_collect_ct", hop_and_count)
        recorder = _Recorder(block_at_the_first_hop)
        with _servers(2) as servers:
            addresses = [address for address, _ in servers]
            try:
                # The receivers finish their hops while the output is blocked.
                with _ctrl_c_when(output_blocked, hops_done) as pressed:
                    recorder.run(_multi_cfg(addresses, iterations=5))
            finally:
                release.set()
            assert pressed.is_set()

        assert sorted(finished) == sorted(str(address) for address in addresses)
        # Only the hop that was being written got out; nothing queued follows the stop.
        assert [event.kind for event in recorder.events][-2:] == ["hop_result", "recon_stopped"]
        assert recorder.events[-1] == recon.ReconStopped(iteration=1, reason="interrupted")
        _assert_stopped_cleanly(runs[0])

    def test_a_connection_completed_after_the_stop_is_closed(self, monkeypatch):
        connecting = threading.Event()
        complete = threading.Event()
        clients: list = []

        class LateClient:
            """Connects only when told to, whatever interrupt() says."""

            def __init__(self, host, port, capture_grace):
                self.info = None
                self.interrupted = self.closed = False
                clients.append(self)

            def connect(self):
                connecting.set()
                assert complete.wait(10.0)
                self.info = object()

            def interrupt(self):
                self.interrupted = True

            def close(self):
                self.closed = True

        monkeypatch.setattr(recon, "RtlTcpClient", LateClient)
        address = RtlTcpAddress("127.0.0.1", 1)
        run = recon._MultiReceiverRun(_cfg(), {address: (98.8e6,)}, recon._Outputs(print))
        slot = run.slots[0]
        thread = threading.Thread(target=run._connect, args=(slot,))
        thread.start()
        assert connecting.wait(10.0)
        assert slot.connecting is clients[0]
        run._stop()
        complete.set()
        thread.join(10.0)

        assert clients[0].interrupted and clients[0].closed
        assert slot.client is None and slot.connecting is None
        run._connect(slot)  # a stopped run connects no more
        assert len(clients) == 2 and slot.client is None


def _restart(port: int) -> tuple[threading.Event, threading.Thread]:
    """A fake server for ``port`` again, once the old one let it go."""
    deadline = time.monotonic() + 2.0
    while True:
        try:
            return _serve_at(port)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _serve_at(port: int) -> tuple[threading.Event, threading.Thread]:
    original = socket.socket.bind

    def bind_to_port(self, address):
        # The old server's closed connections still hold the port in TIME_WAIT.
        self.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return original(self, (address[0], port))

    # _make_fake_rtl_tcp binds port 0; reuse it with the old port instead.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "bind", bind_to_port)
        _, stop, thread = _make_fake_rtl_tcp(_server_bytes(1))
    return stop, thread


class TestReceiverOptions:
    @staticmethod
    def _args(*argv: str):
        return cli.build_parser().parse_args(["recon", *argv])

    def test_items_may_name_their_receiver(self):
        args = self._args(
            "--host",
            "radio",
            "--port",
            "1234",
            "--hop",
            "98.8, 10.0.0.2:1235@102.4,[::1]:1236@98.8",
        )
        assert cli._recon_list_error(args) is None
        cfg = cli._recon_config(args)
        second, third = RtlTcpAddress("10.0.0.2", 1235), RtlTcpAddress("::1", 1236)
        assert cfg.hop_stations == ((None, 98.8e6), (second, 102.4e6), (third, 98.8e6))
        assert cfg.hop_freqs_hz == (98.8e6, 102.4e6, 98.8e6)
        assert cfg.hop_receivers() == {
            RtlTcpAddress("radio", 1234): (98.8e6,),
            second: (102.4e6,),
            third: (98.8e6,),
        }
        assert [str(address) for address in cfg.hop_receivers()] == [
            "radio:1234",
            "10.0.0.2:1235",
            "[::1]:1236",
        ]

    def test_a_plain_list_keeps_its_old_config(self):
        cfg = cli._recon_config(self._args("--hop", "98.8,102.4"))
        assert (cfg.hop_stations, cfg.hop_freqs_hz) == ((), (98.8e6, 102.4e6))
        assert list(cfg.hop_receivers().values()) == [(98.8e6, 102.4e6)]

    @pytest.mark.parametrize(
        ("hop", "message"),
        [
            ("h:0@98.8", "'h:0' has no TCP port (1-65535)"),
            ("h:65536@98.8", "has no TCP port"),
            ("h:x@98.8", "has no TCP port"),
            ("h@98.8", "'h' is not HOST:PORT"),
            (":1235@98.8", "':1235' has no host"),
            ("@98.8", "is not HOST:PORT"),
            ("::1:1235@98.8", "write the IPv6 address of '::1:1235' in brackets"),
            ("[::1]@98.8", "is not [IPV6]:PORT"),
            ("h:1235@x", "could not convert"),
            (
                "h:1235@98.8,h:1235@98.80",
                "list each station once per receiver: 98.80 MHz on h:1235",
            ),
            ("98.8,localhost:1234@98.8", "list each station once per receiver"),
        ],
    )
    def test_unusable_receivers_are_refused(self, hop, message, capsys):
        assert cli.cmd_recon(self._args("--hop", hop)) == 2
        err = capsys.readouterr().err
        assert "--hop: " in err
        assert message in err

    def test_one_station_on_several_receivers_is_allowed(self):
        args = self._args("--hop", "a:1@98.8,b:1@98.8,98.8")
        assert cli._recon_list_error(args) is None
        assert len(cli._recon_config(args).hop_receivers()) == 3

    def test_wide_keeps_one_receiver(self, capsys):
        assert cli.cmd_recon(self._args("--wide", "h:1235@98.8,102.4")) == 2
        assert "HOST:PORT@MHZ applies only to --hop" in capsys.readouterr().err

    def test_the_dashboard_child_gets_the_same_list(self):
        args = self._args("--hop", "98.8,[::1]:1236@102.4,10.0.0.2:1235@98.8")
        argv = cli._recon_child_argv(args)
        assert "--hop=98.8,[::1]:1236@102.4,10.0.0.2:1235@98.8" in argv
        assert cli._recon_config(cli.build_parser().parse_args(argv)) == cli._recon_config(args)


class TestRtlTcpAddress:
    @pytest.mark.parametrize(
        ("text", "host", "port"),
        [
            ("localhost:1234", "localhost", 1234),
            ("[::1]:1235", "::1", 1235),
            ("10.0.0.2:1", "10.0.0.2", 1),
        ],
    )
    def test_parse_and_str_round_trip(self, text, host, port):
        address = RtlTcpAddress.parse(text)
        assert (address.host, address.port, str(address)) == (host, port, text)

    def test_config_derives_the_frequencies(self):
        address = RtlTcpAddress("h", 1)
        cfg = recon.ReconConfig(hop_stations=((address, 98.8e6), (None, 102.4e6)))
        assert cfg.hop_freqs_hz == (98.8e6, 102.4e6)
        assert replace(cfg, hop_stations=(), hop_freqs_hz=(91.0e6,)).hop_freqs_hz == (91.0e6,)


class TestDashboardReceivers:
    def test_old_streams_have_no_receivers(self):
        station = {
            "freq_hz": 98.8e6,
            "rssi_db": -20.0,
            "n_groups": 4,
            "has_ct": True,
            "pi": 0xCAFE,
            "ps_name": "OLD",
        }
        lines = [
            json.dumps(
                {
                    "event": "recon_started",
                    "tuner_type": 5,
                    "gain_count": 29,
                    "ct_timezone_rules": [],
                    "schema": 1,
                }
            ),
            json.dumps(
                {"event": "watchlist_updated", "iteration": 1, "watchlist": [station], "schema": 1}
            ),
        ]
        state, _, _ = _state(lines)
        assert state.receivers == ()
        assert not state.multi_receiver
        assert state.watchlist[0].receiver is None
        assert len(state.view().rows) == 1

    def test_one_receiver_looks_as_before(self):
        receiver = "127.0.0.1:1234"
        station = recon.StationCandidate(98.8e6, -20.0, 4, True, 0xCAFE, "ONE", receiver)
        lines = [
            line(recon.ReconStarted(5, 29, receivers=(receiver,))),
            line(recon.WatchlistUpdated(1, (station,))),
            line(recon.MaintenanceStarted(1, 1)),
        ]
        named, _, _ = _state(lines)
        plain, _, _ = _state(
            [
                line(recon.ReconStarted(5, 29)),
                line(recon.WatchlistUpdated(1, (replace(station, receiver=None),))),
                line(recon.MaintenanceStarted(1, 1)),
            ]
        )
        assert not named.multi_receiver
        assert named.view() == plain.view()

    @pytest.mark.usefixtures("decode_by_server")
    def test_receivers_hearing_different_transmitters_on_one_channel(self):
        recorder = _Recorder()
        with _servers(2) as servers:
            addresses = [address for address, _ in servers]
            hop_stations = tuple((address, 98.8e6) for address in addresses)
            recorder.run(
                _cfg(hop_stations=hop_stations, dwell_s=DWELL_S, settle_s=0.05, iterations=1)
            )

        names = [str(address) for address in addresses]
        assert sorted((hop.receiver, hop.pi) for hop in recorder.of("hop_result")) == sorted(
            [(names[0], PI_BASE), (names[1], PI_BASE + 1)]
        )
        assert recorder.of("cycle_completed")[0].consensus.n_sources == 2

        state, _, _ = _state([line(event) for event in recorder.events])
        rows = {row.cells[0]: row for row in state.view().rows}
        assert set(rows) == set(names)
        for name, pi in zip(names, (PI_BASE, PI_BASE + 1), strict=True):
            cells = dict(zip(state.columns(), rows[name].cells, strict=False))
            assert cells["PI"] == f"0x{pi:04X}"
            # Each row shows the metrics of the transmitter that its receiver heard.
            assert cells["Trust"] != "—" and cells["Age"] != "—"
            assert rows[name].status == "ok"

    @staticmethod
    def _heard(consensus, receiver, freq_hz, pi, mono, wrong_s=0.0, caught_up=True):
        """A hop of ``receiver`` and, when it caught up, its CT in ``consensus``."""
        ct = _clock()
        if caught_up:
            utc = ct.utc + timedelta(seconds=wrong_s)
            consensus.record(
                StationObservation(freq_hz, pi, utc, received_monotonic=mono, receiver=receiver)
            )
        clock_time = ClockTime(ct.utc + timedelta(seconds=wrong_s), 0)
        hop = recon.HopResult(
            freq_hz, pi, "", -20.0, 4, 4, 0, clock_time, caught_up=caught_up, receiver=receiver
        )
        return line(hop)

    @staticmethod
    def _cycle(consensus, iteration, watchlist, now):
        result = consensus.consensus(now)
        tracks = recon._track_snapshots(consensus, now)
        return line(recon.CycleCompleted(iteration, 1, result, watchlist, tracks, 540.0))

    @staticmethod
    def _rows(state):
        return {
            (row.cells[0], row.cells[1]): dict(
                zip(state.columns(), [*row.cells, row.status], strict=True)
            )
            for row in state.view().rows
        }

    def test_co_channel_transmitters_get_their_own_status(self):
        # A hears a wrong clock on 98.8, B the right one there, C sets the majority.
        a, b, c = "10.0.0.1:1234", "10.0.0.2:1234", "10.0.0.3:1234"
        watchlist = (
            recon.StationCandidate(98.8e6, -20.0, 4, True, 0x1001, "", a),
            recon.StationCandidate(98.8e6, -20.0, 4, True, 0x1002, "", b),
            recon.StationCandidate(102.4e6, -20.0, 4, True, 0x1003, "", c),
        )
        consensus = TimeConsensus()
        lines = [
            line(recon.ReconStarted(5, 29, receivers=(a, b, c))),
            line(recon.WatchlistUpdated(1, watchlist)),
            line(recon.MaintenanceStarted(1, 3)),
            self._heard(consensus, a, 98.8e6, 0x1001, 100.0, wrong_s=3600.0),
            self._heard(consensus, b, 98.8e6, 0x1002, 100.0),
            self._heard(consensus, c, 102.4e6, 0x1003, 100.0),
            self._cycle(consensus, 1, watchlist, 101.0),
        ]
        record = json.loads(lines[-1])
        assert record["consensus"]["outlier_freqs_mhz"] == [98.8]
        assert 98.8 in record["consensus"]["contributing_freqs_mhz"]  # both lists, as before

        state, _, _ = _state(lines)
        rows = self._rows(state)
        assert rows[(a, "98.80")]["Status"] == "OUTLIER"
        assert rows[(b, "98.80")]["Status"] == "ok"
        assert rows[(c, "102.40")]["Status"] == "ok"

        # Streams from before the per-track status keep using the frequency lists.
        for entry in record["tracks"]:
            del entry["consensus_status"]
        old, _, _ = _state([*lines[:-1], json.dumps(record)])
        assert {row["Status"] for row in self._rows(old).values() if row["MHz"] == "98.80"} == {
            "OUTLIER"
        }

    @pytest.mark.parametrize("caught_up", [True, False])
    def test_an_unconfirmed_ct_does_not_borrow_the_confirmed_metrics(self, caught_up):
        a, b = "10.0.0.1:1234", "10.0.0.2:1234"
        watchlist = (
            recon.StationCandidate(98.8e6, -20.0, 4, True, 0x1001, "", a),
            recon.StationCandidate(102.4e6, -20.0, 4, True, 0x1002, "", b),
        )
        consensus = TimeConsensus()
        lines = [
            line(recon.ReconStarted(5, 29, receivers=(a, b))),
            line(recon.WatchlistUpdated(1, watchlist)),
            line(recon.MaintenanceStarted(1, 2)),
            self._heard(consensus, a, 98.8e6, 0x1001, 98.0),
            self._heard(consensus, b, 102.4e6, 0x1002, 99.0),
            self._cycle(consensus, 1, watchlist, 100.0),
            line(recon.MaintenanceStarted(2, 2)),
            # Later the row's CT arrives without a confirmed PI.
            self._heard(consensus, a, 98.8e6, None, 168.0, caught_up=caught_up),
            self._heard(consensus, b, 102.4e6, 0x1002, 169.0),
            self._cycle(consensus, 2, watchlist, 170.0),
        ]
        state, _, _ = _state(lines)
        row = self._rows(state)[(a, "98.80")]
        assert row["PI"] == "—"
        if caught_up:
            # The consensus counts the unconfirmed track: its metrics, not P's (age 72 s).
            assert (row["Age"], row["Status"]) == ("2 s", "ok")
        else:
            # This CT never entered the consensus: no track holds its metrics.
            assert (row["Age"], row["Trust"], row["Status"]) == ("—", "—", "")

    def test_a_failed_receiver_is_logged_until_it_is_back(self):
        failed = line(recon.ReceiverFailed(2, "10.0.0.2:1235", "Connection refused"))
        state, _, _ = _state([line(recon.ReconStarted(5, 29))])
        assert state.apply_line(failed) == [
            "Receiver 10.0.0.2:1235 failed: Connection refused; reconnecting next cycle."
        ]
        assert state.failed_receivers == {"10.0.0.2:1235": "Connection refused"}
        hop = recon.HopResult(98.8e6, 0xCAFE, "A", -20.0, 4, 4, 0, None, receiver="10.0.0.2:1235")
        assert state.apply_line(line(hop))[0] == "Receiver 10.0.0.2:1235 is working again."
        assert state.failed_receivers == {}
