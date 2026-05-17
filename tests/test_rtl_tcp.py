"""Lightweight rtl_tcp client test - no real server (fake TCP server)."""

import socket
import struct
import threading

import numpy as np

from rdsclock.rtl_tcp import RtlTcpClient


def _make_fake_server(samples: bytes, min_cmds: int = 3):
    """Start a fake rtl_tcp listener on an ephemeral port. Returns (port, list).

    It sends `samples` only AFTER receiving at least `min_cmds` commands from
    the client, so the tests can verify that all commands arrived.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    received_cmds: list = []

    def serve():
        conn, _ = srv.accept()
        try:
            conn.sendall(b"RTL0" + struct.pack(">II", 5, 29))
            buf = b""
            sent = False
            while True:
                conn.settimeout(2.0)
                try:
                    chunk = conn.recv(4096)
                except TimeoutError:
                    if not sent:
                        # Even if we did not get min_cmds, send what we have
                        conn.sendall(samples)
                        sent = True
                    break
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 5:
                    cmd, val = struct.unpack(">BI", buf[:5])
                    received_cmds.append((cmd, val))
                    buf = buf[5:]
                if not sent and len(received_cmds) >= min_cmds:
                    conn.sendall(samples)
                    sent = True
        finally:
            conn.close()
            srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return port, received_cmds


def test_connect_and_header():
    samples = bytes([127, 127] * 100)
    port, _ = _make_fake_server(samples)
    with RtlTcpClient(host="127.0.0.1", port=port) as client:
        assert client.info is not None
        assert client.info.magic == b"RTL0"
        assert client.info.tuner_type == 5
        assert client.info.gain_count == 29


def test_set_freq_and_read():
    samples = bytes([127, 127] * 100)
    port, cmds = _make_fake_server(samples)
    with RtlTcpClient(host="127.0.0.1", port=port) as client:
        client.set_sample_rate(250_000)
        client.set_frequency(95_500_000)
        client.set_gain_mode_auto()
        iq = client.read_iq(100, settle_s=0.0)
        assert len(iq) == 100
        # 127/127.5 ~= 0.996, bias 127.5 -> (127-127.5)/127.5 ~= -0.004
        assert np.allclose(np.abs(iq), 0.0055, atol=0.01)
    # Verify that the commands arrived
    assert (2, 250_000) in cmds
    assert (1, 95_500_000) in cmds
    assert (3, 0) in cmds
