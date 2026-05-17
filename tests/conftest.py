"""Pytest configuration and shared test fixtures."""

import contextlib
import os
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


def _make_fake_rtl_tcp(samples_per_session: bytes, max_sessions: int = 10):
    """Start a fake rtl_tcp listener for CLI and recon tests."""
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
                conn.settimeout(0.5)
                data = samples_per_session * 4
                with contextlib.suppress(BrokenPipeError, OSError):
                    conn.sendall(data)
                while not stop.is_set():
                    try:
                        chunk = conn.recv(4096)
                    except TimeoutError:
                        continue
                    if not chunk:
                        break
            finally:
                with contextlib.suppress(OSError):
                    conn.close()
        with contextlib.suppress(OSError):
            srv.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, stop


def _white_noise_samples(n_samples: int = 2_000_000) -> bytes:
    """Generate deterministic uint8 IQ centred at 128."""
    rng = np.random.default_rng(0)
    arr = rng.integers(64, 192, size=n_samples * 2, dtype=np.uint8)
    return arr.tobytes()


@pytest.fixture
def fake_rtl_tcp():
    port, stop = _make_fake_rtl_tcp(_white_noise_samples())
    yield port
    stop.set()
    time.sleep(0.1)
