"""Pytest configuration and shared test fixtures."""

import contextlib
import os
import select
import socket
import struct
import sys
import threading
import time

import numpy as np
import pytest

HERE = os.path.dirname(__file__)
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
SRC = os.path.join(PROJECT_ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _make_fake_rtl_tcp(samples_per_session: bytes, max_sessions: int = 10, backlog_s: float = 0.0):
    """Continuously stream IQ in timed bursts, including between captures.

    With ``backlog_s``, the first frequency command of a session leaves that
    much of the stream queued, as rtl_tcp queues samples while its client is
    busy. Each burst then also carries up to 1.5 bursts of the queue, so the
    stream arrives faster than real time, with quiet gaps between bursts.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(max_sessions)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve():
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            try:
                conn.sendall(b"RTL0" + struct.pack(">II", 5, 29))
                conn.settimeout(0.2)
                sample_rate = 250_000
                commands = bytearray()
                offset = 0
                queued = 0
                backlog_due = backlog_s > 0
                next_send = time.monotonic()
                while not stop.is_set():
                    readable, _, _ = select.select(
                        [conn], [], [], max(0, next_send - time.monotonic())
                    )
                    if readable:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        commands.extend(chunk)
                        while len(commands) >= 5:
                            command, value = struct.unpack(">BI", commands[:5])
                            del commands[:5]
                            if command == 2:
                                sample_rate = value
                            elif command == 1 and backlog_due:
                                queued = int(sample_rate * backlog_s) * 2
                                backlog_due = False
                    if time.monotonic() >= next_send:
                        count = max(2, int(sample_rate * 0.1) * 2)
                        extra = min(queued, count * 3 // 4 * 2)
                        queued -= extra
                        count += extra
                        repeats = (offset + count + len(samples_per_session) - 1) // len(
                            samples_per_session
                        )
                        data = (samples_per_session * repeats)[offset : offset + count]
                        conn.sendall(data)
                        offset = (offset + count) % len(samples_per_session)
                        next_send = time.monotonic() + 0.1
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    conn.close()
        with contextlib.suppress(OSError):
            srv.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, stop, thread


def _white_noise_samples(n_samples: int = 2_000_000) -> bytes:
    """Generate deterministic uint8 IQ centred at 128."""
    rng = np.random.default_rng(0)
    arr = rng.integers(64, 192, size=n_samples * 2, dtype=np.uint8)
    return arr.tobytes()


@pytest.fixture
def rtl_tcp_factory():
    @contextlib.contextmanager
    def receiver(samples, backlog_s=0.0):
        port, stop, thread = _make_fake_rtl_tcp(samples, backlog_s=backlog_s)
        try:
            yield port
        finally:
            stop.set()
            thread.join(timeout=1.0)
            assert not thread.is_alive()

    return receiver


@pytest.fixture
def fake_rtl_tcp(rtl_tcp_factory):
    with rtl_tcp_factory(_white_noise_samples()) as port:
        yield port
