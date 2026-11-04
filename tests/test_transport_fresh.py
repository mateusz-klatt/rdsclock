"""Receiver lifecycle and fresh-capture tests using local sockets only."""

import itertools
import socket
import struct
import threading
import time
from unittest.mock import Mock

import numpy as np
import pytest

from rdsclock import rtl_tcp
from rdsclock.rtl_tcp import RtlTcpClient, RtlTcpDrainTimeoutError


@pytest.fixture
def socket_client():
    receiver, sender = socket.socketpair()
    receiver.settimeout(1)
    sender.settimeout(1)
    client = RtlTcpClient()
    client._sock = receiver
    try:
        yield client, sender
    finally:
        client.close()
        sender.close()


@pytest.mark.parametrize("failure", ["timeout", "truncated", "bad_magic"])
def test_failed_handshake_closes_socket_even_when_exception_is_retained(monkeypatch, failure):
    receiver, sender = socket.socketpair()
    client = RtlTcpClient(read_timeout=0.01)
    monkeypatch.setattr(rtl_tcp.socket, "create_connection", lambda *args, **kwargs: receiver)
    try:
        if failure == "truncated":
            sender.sendall(b"RTL0")
            sender.shutdown(socket.SHUT_WR)
        elif failure == "bad_magic":
            sender.sendall(b"NOPE" + struct.pack(">II", 5, 29))
        with pytest.raises(OSError) as retained_error:
            client.connect()
        assert retained_error.value.__traceback__ is not None
        assert receiver.fileno() == -1
        assert client._sock is None
        client.close()
        sender.settimeout(0.2)
        assert sender.recv(1) == b""
    finally:
        receiver.close()
        sender.close()


@pytest.mark.parametrize("gain_db, expected_gain", [(None, None), (19.7, 197), (19.76, 198)])
def test_fm_profile_resets_inherited_receiver_state(socket_client, gain_db, expected_gain):
    client, sender = socket_client
    state = {2: 1_024_000, 3: 1, 4: 496, 5: 23, 7: 1, 8: 1, 9: 2}
    client.configure_fm(250_000, gain_db=gain_db, ppm=0)
    count = 6 if gain_db is None else 7
    data = RtlTcpClient._recv_exact(sender, count * 5)
    for offset in range(0, len(data), 5):
        command, value = struct.unpack(">BI", data[offset : offset + 5])
        assert command in state
        state[command] = value
    assert state[2] == 250_000
    assert state[5] == 0  # Zero must override another client's correction.
    assert state[7] == state[8] == state[9] == 0
    assert state[3] == (0 if gain_db is None else 1)
    if expected_gain is not None:
        assert state[4] == expected_gain


def _send_on_capture(monkeypatch, client, sender, *bursts):
    """Hold future samples until draining finishes; no timing race is needed.

    Each burst is ``(seconds after the capture read starts, data)``.
    """
    collecting = threading.Event()
    read_capture = client._read_capture

    def mark_capture(*args, **kwargs):
        collecting.set()
        return read_capture(*args, **kwargs)

    monkeypatch.setattr(client, "_read_capture", mark_capture)

    def send():
        if not collecting.wait(2):
            return
        started = time.monotonic()
        for after_s, data in bursts:
            time.sleep(max(0.0, started + after_s - time.monotonic()))
            sender.sendall(data)

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    return worker


FRESH_PAIR = bytes([200, 130])


def _stream(n_samples, sample_rate, *, speed=1.0, after_s=0.0, chunk=10):
    """Bursts of ``chunk`` samples, each sent once its last sample exists.

    ``speed`` above 1 delivers a queue faster than real time.
    """
    return [
        (after_s + (first + chunk) / (sample_rate * speed), FRESH_PAIR * chunk)
        for first in range(0, n_samples, chunk)
    ]


def test_fresh_capture_discards_queued_station_before_full_recording(socket_client, monkeypatch):
    client, sender = socket_client
    sender.sendall(bytes([10, 20]) * 100)
    worker = _send_on_capture(monkeypatch, client, sender, (0.0, bytes([200, 130]) * 5))
    before = time.monotonic_ns()
    capture = client.capture_fresh(0.005, 1000, settle_s=0.01, quiet_s=0.01)
    worker.join(1)
    assert not worker.is_alive()
    assert capture.discarded_samples == 100
    assert len(capture.iq) == 5
    assert capture.iq.dtype == np.complex64
    assert np.allclose(capture.iq.real, (200 - 127.5) / 127.5)
    assert np.allclose(capture.iq.imag, (130 - 127.5) / 127.5)
    assert before <= capture.capture_start_monotonic_ns <= time.monotonic_ns()


def test_drain_preserves_iq_alignment_across_partial_network_reads(socket_client, monkeypatch):
    client, sender = socket_client
    sender.sendall(bytes([10, 20, 30]))  # Last Q byte has not arrived yet.
    quiet_detected = threading.Event()
    collecting = threading.Event()
    real_select = rtl_tcp.select.select
    read_capture = client._read_capture

    def observe_quiet(*args):
        result = real_select(*args)
        if not result[0]:
            quiet_detected.set()
        return result

    def mark_capture(*args, **kwargs):
        collecting.set()
        return read_capture(*args, **kwargs)

    monkeypatch.setattr(rtl_tcp.select, "select", observe_quiet)
    monkeypatch.setattr(client, "_read_capture", mark_capture)

    def send():
        if quiet_detected.wait(2):
            sender.sendall(bytes([40]))
        if collecting.wait(2):
            sender.sendall(bytes([200, 130]) * 5)

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    capture = client.capture_fresh(0.005, 1000, settle_s=0, quiet_s=0.01)
    worker.join(1)
    assert not worker.is_alive()
    assert capture.discarded_samples == 2
    assert len(capture.iq) == 5
    assert np.allclose(capture.iq.real, (200 - 127.5) / 127.5)
    assert np.allclose(capture.iq.imag, (130 - 127.5) / 127.5)


def test_continuously_backlogged_receiver_fails_instead_of_returning_stale_iq(monkeypatch):
    client = RtlTcpClient()
    receiver = Mock(spec=socket.socket)
    receiver.recv.return_value = bytes([10, 20]) * 100
    client._sock = receiver
    clock = itertools.count(0, 0.001)
    monkeypatch.setattr(rtl_tcp.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(rtl_tcp.select, "select", lambda *args: ([receiver], [], []))
    read_capture = Mock()
    monkeypatch.setattr(client, "_read_capture", read_capture)
    with pytest.raises(RtlTcpDrainTimeoutError, match="no fresh capture boundary"):
        client.capture_fresh(0.1, 250_000, settle_s=0.01, max_drain_s=0.05)
    read_capture.assert_not_called()
    assert receiver.recv.call_count > 0


def test_high_rate_capture_uses_short_gaps_in_a_continuous_stream(socket_client):
    client, sender = socket_client
    sender.sendall(bytes([10, 20]) * 100)
    stop = threading.Event()
    started = threading.Event()

    def send():
        # 2400 samples every millisecond models a 2.4 MS/s stream. There is
        # no 50 ms quiet period; the adaptive default must permit capture.
        try:
            while not stop.is_set():
                sender.sendall(bytes([200, 130]) * 2400)
                started.set()
                stop.wait(0.001)
        except OSError:
            pass

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    try:
        assert started.wait(1)
        capture = client.capture_fresh(0.005, 2_400_000, settle_s=0.03, max_drain_s=0.5)
        assert capture.discarded_samples >= 100
        assert len(capture.iq) == 12_000
        assert np.allclose(capture.iq.real, (200 - 127.5) / 127.5)
        assert np.allclose(capture.iq.imag, (130 - 127.5) / 127.5)
        # A stream that keeps up is not reported, whatever its burst size.
        assert not capture.backlogged
        assert capture.caught_up
    finally:
        stop.set()
        worker.join(2)
        assert not worker.is_alive()


def _capture(monkeypatch, client, sender, duration_s, *bursts, threshold_s=0.1):
    """Capture at 1 kS/s with 50 ms transfers.

    A queue of more than 0.1 s then has to be seen draining: the last 0.4 s of
    the read must show the stream in real time.
    """
    worker = _send_on_capture(monkeypatch, client, sender, *bursts)
    capture = client.capture_fresh(
        duration_s,
        1000,
        settle_s=0,
        quiet_s=0.01,
        backlog_threshold_s=threshold_s,
        transfer_samples=50,
    )
    worker.join(1)
    assert not worker.is_alive()
    return capture, (capture.capture_end_monotonic_ns - capture.capture_start_monotonic_ns) / 1e9


def test_samples_that_arrive_together_are_dated_by_their_duration(socket_client, monkeypatch):
    # 500 samples at 1 kS/s arrive at once only if they had queued: the last
    # one exists 0.5 s after the first. The stream never showed real time, so
    # the samples may be older still.
    client, sender = socket_client
    capture, span_s = _capture(monkeypatch, client, sender, 0.5, (0.0, FRESH_PAIR * 500))
    assert 0.4 <= capture.queued_s <= 0.5
    assert capture.backlogged
    assert not capture.caught_up
    assert span_s >= 0.5
    assert np.allclose(capture.iq.real, (200 - 127.5) / 127.5)
    assert np.allclose(capture.iq.imag, (130 - 127.5) / 127.5)


def test_a_capture_that_ends_ahead_of_real_time_has_not_caught_up(socket_client, monkeypatch):
    # A backlog longer than the capture: 0.5 s of samples arrive in 0.1 s, so
    # every read moves the start back and the true start stays unknown.
    client, sender = socket_client
    bursts = _stream(500, 1000, speed=5, chunk=50)
    capture, _ = _capture(monkeypatch, client, sender, 0.5, *bursts)
    assert capture.backlogged
    assert not capture.caught_up
    assert capture.queued_s <= 0.4 + 1e-3  # only a lower bound on the queue


def test_a_short_capture_ahead_of_real_time_has_not_caught_up(socket_client, monkeypatch):
    # 0.2 s delivered in 0.04 s queued 0.16 s: too little to report as a
    # backlog, but more than one transfer, and never seen in real time.
    client, sender = socket_client
    bursts = _stream(200, 1000, speed=5, chunk=50)
    capture, _ = _capture(monkeypatch, client, sender, 0.2, *bursts, threshold_s=0.25)
    assert 0.1 < capture.queued_s < 0.25
    assert not capture.backlogged
    assert not capture.caught_up


def test_a_stream_that_keeps_up_is_dated_from_the_read(socket_client, monkeypatch):
    client, sender = socket_client
    before = time.monotonic_ns()
    capture, span_s = _capture(monkeypatch, client, sender, 0.5, *_stream(500, 1000))
    assert capture.queued_s < 0.005
    assert not capture.backlogged
    assert capture.caught_up
    assert before <= capture.capture_start_monotonic_ns
    assert span_s >= 0.5


def test_samples_lost_mid_capture_do_not_move_the_start(socket_client, monkeypatch):
    # 300 samples never arrive. Dating the capture from its end would make the
    # 250 samples before the gap 0.3 s late.
    client, sender = socket_client
    bursts = _stream(250, 1000) + _stream(250, 1000, after_s=0.55)
    capture, span_s = _capture(monkeypatch, client, sender, 0.5, *bursts)
    assert capture.queued_s < 0.005
    assert capture.caught_up
    assert span_s - 0.5 >= 0.29  # the lost time shows in the span


def test_a_backlog_moves_the_start_back_and_a_later_loss_does_not(socket_client, monkeypatch):
    # 200 queued samples arrive together, the stream keeps up, then 20 samples
    # are lost. Only the backlog dates the capture: its end would leave every
    # sample 0.02 s late.
    client, sender = socket_client
    bursts = [
        (0.0, FRESH_PAIR * 200),
        *_stream(300, 1000),
        *_stream(200, 1000, after_s=0.32),
    ]
    capture, span_s = _capture(monkeypatch, client, sender, 0.7, *bursts)
    assert 0.15 <= capture.queued_s <= 0.201
    assert capture.backlogged
    assert capture.caught_up  # 0.5 s in real time after the backlog
    assert span_s - 0.7 >= 0.015


@pytest.mark.parametrize(
    "bursts",
    [
        # Twice real time, a 0.6 s stall, twice real time again: the bound
        # never falls below its old low, but the queue is still draining.
        [
            *_stream(800, 1000, speed=2, chunk=20),
            *_stream(800, 1000, speed=2, chunk=20, after_s=1.0),
        ],
        # A backlog, 0.4 s in real time, then the last samples 0.3 s late.
        [(0.0, FRESH_PAIR * 200), *_stream(400, 1000), (0.71, FRESH_PAIR * 10)],
        # A stall long enough to leave half of the final window without reads.
        [(0.0, FRESH_PAIR * 200), *_stream(300, 1000), (0.81, FRESH_PAIR * 10)],
        # Twice real time for 0.56 s, a 0.16 s stall, then twice real time
        # for 0.08 s: the stall falls inside the final window.
        [
            *_stream(1120, 1000, speed=2, chunk=20),
            *_stream(160, 1000, speed=2, chunk=20, after_s=0.72),
        ],
    ],
    ids=[
        "ahead_again_after_a_stall",
        "stall_before_the_end",
        "stall_through_the_window",
        "stall_inside_the_window",
    ],
)
def test_a_stall_is_not_taken_for_catching_up(socket_client, monkeypatch, bursts):
    client, sender = socket_client
    n_samples = sum(len(data) for _, data in bursts) // 2
    capture, _ = _capture(monkeypatch, client, sender, n_samples / 1000, *bursts)
    assert capture.backlogged
    assert not capture.caught_up


def _capture_behind_a_backlog(rtl_tcp_factory, duration_s):
    # As on the live receiver on 2026-11-03: the drain stops in a gap between
    # bursts of rtl_tcp's queue, and the capture reads a second of queued
    # samples faster than real time.
    with (
        rtl_tcp_factory(FRESH_PAIR * 4096, backlog_s=1.0) as port,
        RtlTcpClient("127.0.0.1", port) as client,
    ):
        client.set_sample_rate(2_400_000)
        client.set_frequency(102_850_000)
        # The fake server sends 0.1 s of samples per burst.
        capture = client.capture_fresh(duration_s, 2_400_000, settle_s=0, transfer_samples=240_000)
    return capture, (capture.capture_end_monotonic_ns - capture.capture_start_monotonic_ns) / 1e9


def test_a_backlog_sent_between_quiet_gaps_is_dated_by_its_arrival(rtl_tcp_factory):
    capture, span_s = _capture_behind_a_backlog(rtl_tcp_factory, 3.0)
    assert span_s - capture.queued_s < 2.5  # the read itself was that much shorter
    assert capture.backlogged
    assert capture.caught_up
    assert 0.6 <= capture.queued_s <= 1.4
    assert 3.0 <= span_s < 3.3  # dated by arrival, the samples span real time again


def test_a_capture_shorter_than_the_backlog_does_not_catch_up(rtl_tcp_factory):
    capture, _ = _capture_behind_a_backlog(rtl_tcp_factory, 0.75)
    assert capture.backlogged
    assert not capture.caught_up
    assert capture.queued_s < 0.75  # a lower bound: about a second had queued


def test_fresh_capture_reports_eof_during_drain(socket_client):
    client, sender = socket_client
    sender.sendall(bytes([10, 20]) * 100)
    sender.shutdown(socket.SHUT_WR)
    with pytest.raises(OSError, match="socket closed while discarding"):
        client.capture_fresh(0.01, 250_000)


def test_eof_during_the_capture_read_closes_the_connection(socket_client, monkeypatch):
    client, sender = socket_client
    receiver = client._sock
    read_capture = client._read_capture

    def end_stream(*args, **kwargs):
        sender.sendall(FRESH_PAIR * 3)
        sender.shutdown(socket.SHUT_WR)
        return read_capture(*args, **kwargs)

    monkeypatch.setattr(client, "_read_capture", end_stream)
    with pytest.raises(OSError, match="rtl_tcp: socket closed$"):
        client.capture_fresh(0.005, 1000, settle_s=0, quiet_s=0.001)
    assert receiver.fileno() == -1
    assert client._sock is None


@pytest.mark.parametrize("phase", ["drain", "read"])
def test_partial_iq_timeout_requires_reconnect_before_an_aligned_capture(
    socket_client, monkeypatch, phase
):
    client, sender = socket_client
    original_receiver = client._sock
    original_receiver.settimeout(0.02)
    partial_pair = bytes([10, 20, 30])
    with monkeypatch.context() as capture_patch:
        if phase == "drain":
            sender.sendall(partial_pair)
            expected_error = RtlTcpDrainTimeoutError
        else:
            worker = _send_on_capture(capture_patch, client, sender, (0.0, partial_pair))
            expected_error = TimeoutError
        with pytest.raises(expected_error):
            client.capture_fresh(0.002, 1000, settle_s=0, quiet_s=0.001, max_drain_s=0.02)
        if phase == "read":
            worker.join(1)
            assert not worker.is_alive()

    assert original_receiver.fileno() == -1
    assert client._sock is None
    with pytest.raises(RuntimeError, match="not connected"):
        client.capture_fresh(0.005, 1000, settle_s=0)

    new_receiver, new_sender = socket.socketpair()
    try:
        new_sender.sendall(b"RTL0" + struct.pack(">II", 5, 29))
        monkeypatch.setattr(
            rtl_tcp.socket, "create_connection", lambda *args, **kwargs: new_receiver
        )
        client.connect()
        worker = _send_on_capture(monkeypatch, client, new_sender, (0.0, bytes([200, 130]) * 5))
        capture = client.capture_fresh(0.005, 1000, settle_s=0, quiet_s=0.001)
        worker.join(1)
        assert not worker.is_alive()
        assert len(capture.iq) == 5
        assert np.allclose(capture.iq.real, (200 - 127.5) / 127.5)
        assert np.allclose(capture.iq.imag, (130 - 127.5) / 127.5)
    finally:
        client.close()
        new_receiver.close()
        new_sender.close()


@pytest.mark.parametrize("phase", ["drain", "read"])
def test_capture_interruption_closes_connection(socket_client, monkeypatch, phase):
    client, _ = socket_client
    receiver = client._sock

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    if phase == "drain":
        monkeypatch.setattr(client, "_discard_until_quiet", interrupt)
    else:
        monkeypatch.setattr(client, "_discard_until_quiet", lambda *args: 0)
        monkeypatch.setattr(client, "_read_capture", interrupt)
    with pytest.raises(KeyboardInterrupt):
        client.capture_fresh(0.005, 1000)
    assert receiver.fileno() == -1
    assert client._sock is None


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("duration_s", -1),
        ("backlog_threshold_s", 0.0),
        ("backlog_threshold_s", -0.1),
        ("backlog_threshold_s", float("nan")),
        ("transfer_samples", 0),
        ("transfer_samples", 2.5),
    ],
)
def test_invalid_capture_arguments_leave_connection_usable(socket_client, argument, value):
    client, sender = socket_client
    arguments = {"duration_s": 0.005, "sample_rate": 1000, argument: value}
    with pytest.raises(ValueError, match=argument):
        client.capture_fresh(**arguments)
    sender.sendall(bytes([200, 130]))
    iq = client.read_iq(1, settle_s=0)
    assert np.allclose(iq.real, (200 - 127.5) / 127.5)
    assert np.allclose(iq.imag, (130 - 127.5) / 127.5)
