"""CLI tests for sub-commands that talk to ``rtl_tcp``.

We spin up a tiny in-process fake server that replays the rtl_tcp wire
protocol (greeting, command sink, sample dump) and point the CLI at
``localhost:<ephemeral>``. This exercises ``cmd_live``, ``cmd_multi``,
``cmd_scan`` and ``cmd_recon`` without needing real hardware.
"""

import contextlib
import socket
import struct
import threading
import time

import numpy as np
import pytest

from rdsclock.cli import main


def _make_fake_rtl_tcp(samples_per_session: bytes, max_sessions: int = 10):
    """Start a fake rtl_tcp listener.

    Each TCP connection: send the 12-byte greeting, accept any
    incoming commands, then immediately dump ``samples_per_session``
    repeated as needed until the client disconnects.
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
                conn.settimeout(0.5)
                # Pre-queue a generous amount of samples so multiple
                # read_iq() / record() calls all succeed.
                data = samples_per_session * 4
                with contextlib.suppress(BrokenPipeError, OSError):
                    conn.sendall(data)
                # Drain commands until the client closes.
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

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return port, stop


def _white_noise_samples(n_samples: int = 2_000_000) -> bytes:
    """Generate n_samples of uint8 IQ centred at 128 (zero-mean noise)."""
    rng = np.random.default_rng(0)
    arr = rng.integers(64, 192, size=n_samples * 2, dtype=np.uint8)
    return arr.tobytes()


@pytest.fixture
def fake_rtl_tcp():
    port, stop = _make_fake_rtl_tcp(_white_noise_samples())
    yield port
    stop.set()
    time.sleep(0.1)


class TestCmdLive:
    def test_default_run(self, fake_rtl_tcp, capsys):
        rc = main(
            [
                "live",
                "--freq",
                "95.5",
                "--duration",
                "1.0",
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Recording" in out
        assert "Groups:" in out

    def test_with_manual_gain_and_carrier(self, fake_rtl_tcp, tmp_path):
        save_path = tmp_path / "saved.iq"
        rc = main(
            [
                "live",
                "--freq",
                "95.5",
                "--duration",
                "1.0",
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
                "--gain",
                "30",
                "--ppm",
                "5",
                "--carrier-hz",
                "57000",
                "--save",
                str(save_path),
                "-v",
            ]
        )
        assert rc == 0
        assert save_path.exists()


class TestCmdScan:
    def test_short_sweep(self, fake_rtl_tcp, capsys):
        rc = main(
            [
                "scan",
                "--start",
                "95.5",
                "--end",
                "95.7",
                "--step",
                "0.1",
                "--duration",
                "0.5",
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
                "--gain",
                "30",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Scanning" in out


class TestCmdMulti:
    def test_wide_mode(self, fake_rtl_tcp, capsys):
        rc = main(
            [
                "multi",
                "--freqs",
                "95.5,96.0",
                "--mode",
                "wide",
                "--fs",
                "2400000",
                "--duration",
                "0.5",
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "WIDE mode" in out

    def test_hop_mode(self, fake_rtl_tcp, capsys):
        rc = main(
            [
                "multi",
                "--freqs",
                "92.0,98.3,106.8",
                "--mode",
                "hop",
                "--duration",
                "0.5",
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "HOP mode" in out

    def test_wide_rejected_when_too_narrow(self, fake_rtl_tcp, capsys):
        # 92.0 / 98.3 / 106.8 span 14.8 MHz, far above 2.4 MS/s.
        rc = main(
            [
                "multi",
                "--freqs",
                "92.0,98.3,106.8",
                "--mode",
                "wide",
                "--fs",
                "2400000",
                "--duration",
                "0.5",
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
            ]
        )
        assert rc == 2


class TestCmdRecon:
    def test_one_iteration(self, fake_rtl_tcp, capsys):
        rc = main(
            [
                "recon",
                "--start",
                "95.5",
                "--end",
                "95.7",
                "--step",
                "0.1",
                "--scan-dwell",
                "0.5",
                "--dwell",
                "1.0",
                "--idle",
                "0",
                "--iterations",
                "1",
                "--rssi-threshold",
                "-200",  # accept anything
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
                "--gain",
                "30",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "ACQUISITION" in out
        assert "MAINTENANCE" in out

    def test_offline_mode_via_cli(self, tmp_path, capsys):
        # First synthesise two IQ files into a fresh directory.
        iq_dir = tmp_path / "iq"
        iq_dir.mkdir()
        for freq in ["fm_092.00_MHz.iq", "fm_098.30_MHz.iq"]:
            main(
                [
                    "generate",
                    str(iq_dir / freq),
                    "--time",
                    "2026-05-17T12:00",
                    "--duration",
                    "2.0",
                    "--snr",
                    "30",
                    "--seed",
                    "0",
                ]
            )
        capsys.readouterr()  # discard generate output

        rc = main(["recon", "--from-dir", str(iq_dir)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "OFFLINE RECON" in out
