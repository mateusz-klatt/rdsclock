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


def _send_on_capture(monkeypatch, client, sender, fresh):
    """Hold future samples until draining finishes; no timing race is needed."""
    collecting = threading.Event()
    read_iq = client.read_iq

    def mark_capture(*args, **kwargs):
        collecting.set()
        return read_iq(*args, **kwargs)

    monkeypatch.setattr(client, "read_iq", mark_capture)

    def send():
        if collecting.wait(2):
            sender.sendall(fresh)

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    return worker


def test_fresh_capture_discards_queued_station_before_full_recording(socket_client, monkeypatch):
    client, sender = socket_client
    sender.sendall(bytes([10, 20]) * 100)
    worker = _send_on_capture(monkeypatch, client, sender, bytes([200, 130]) * 5)
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
    read_iq = client.read_iq

    def observe_quiet(*args):
        result = real_select(*args)
        if not result[0]:
            quiet_detected.set()
        return result

    def mark_capture(*args, **kwargs):
        collecting.set()
        return read_iq(*args, **kwargs)

    monkeypatch.setattr(rtl_tcp.select, "select", observe_quiet)
    monkeypatch.setattr(client, "read_iq", mark_capture)

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
    read_iq = Mock()
    monkeypatch.setattr(client, "read_iq", read_iq)
    with pytest.raises(RtlTcpDrainTimeoutError, match="no fresh capture boundary"):
        client.capture_fresh(0.1, 250_000, settle_s=0.01, max_drain_s=0.05)
    read_iq.assert_not_called()
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
    finally:
        stop.set()
        worker.join(2)
        assert not worker.is_alive()


def test_fresh_capture_reports_eof_during_drain(socket_client):
    client, sender = socket_client
    sender.sendall(bytes([10, 20]) * 100)
    sender.shutdown(socket.SHUT_WR)
    with pytest.raises(OSError, match="socket closed while discarding"):
        client.capture_fresh(0.01, 250_000)


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
            worker = _send_on_capture(capture_patch, client, sender, partial_pair)
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
        worker = _send_on_capture(monkeypatch, client, new_sender, bytes([200, 130]) * 5)
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
        monkeypatch.setattr(client, "read_iq", interrupt)
    with pytest.raises(KeyboardInterrupt):
        client.capture_fresh(0.005, 1000)
    assert receiver.fileno() == -1
    assert client._sock is None


def test_invalid_capture_arguments_leave_connection_usable(socket_client):
    client, sender = socket_client
    with pytest.raises(ValueError, match="duration_s"):
        client.capture_fresh(-1, 1000)
    sender.sendall(bytes([200, 130]))
    iq = client.read_iq(1, settle_s=0)
    assert np.allclose(iq.real, (200 - 127.5) / 127.5)
    assert np.allclose(iq.imag, (130 - 127.5) / 127.5)
