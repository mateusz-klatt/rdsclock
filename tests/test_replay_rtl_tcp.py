"""tools/replay_rtl_tcp.py: a recorded IQ file served as an rtl_tcp receiver."""

import socket
import struct
import threading
import time
from datetime import UTC, datetime

import numpy as np
import pytest

from rdsclock.decoder import decode_iq
from rdsclock.dsp import write_iq_complex64
from rdsclock.rds_groups import encode_group_4a, encode_ps_groups
from rdsclock.rtl_tcp import RtlTcpClient
from rdsclock.synth import synthesize_fm_iq
from tools import replay_rtl_tcp as replay

FS = 250_000
PI = 0x3A5C
CT = datetime(2026, 5, 17, 12, 34, tzinfo=UTC)


class _Server:
    """A :class:`replay.ReplayServer` in a thread, with its log."""

    def __init__(self, path, fmt, **kwargs):
        self.log: list[str] = []
        source = replay.IqSource(path, fmt, log=self.log.append)
        self.server = replay.ReplayServer(source, port=0, log=self.log.append, **kwargs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.thread.join(timeout=2.0)

    def wait_for(self, text: str, timeout_s: float = 2.0) -> str:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for entry in list(self.log):
                if text in entry:
                    return entry
            time.sleep(0.02)
        raise AssertionError(f"{text!r} not in {self.log}")


def _receive_all(port: int) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


@pytest.fixture(scope="module")
def rds_cf32(tmp_path_factory):
    """Two seconds of synthetic RDS with PI and CT, scaled to stay within ±1."""
    groups = encode_ps_groups(PI, "REPLAY  ") + [encode_group_4a(PI, clock_time_local=CT)]
    iq = synthesize_fm_iq(groups, duration_s=2.0, fs=FS, rng=np.random.default_rng(1))
    path = tmp_path_factory.mktemp("replay") / "rds.cf32"
    write_iq_complex64(0.9 * iq / np.max(np.abs(iq)), str(path))
    return path


def test_rdsclock_decodes_a_replayed_recording(rds_cf32):
    with _Server(rds_cf32, "cf32", loop=True) as served:
        with RtlTcpClient("127.0.0.1", served.server.port) as client:
            assert (client.info.tuner_type, client.info.gain_count) == (5, 29)
            client.configure_fm(FS)
            client.set_frequency(98_800_000)
            capture = client.capture_fresh(1.2, FS, settle_s=0.1)
        served.wait_for("client: set frequency 98.800 MHz")
        served.wait_for("client disconnected")

    result = decode_iq(capture.iq, fs=FS)
    assert result.info.pi == PI
    assert result.info.ps_name == "REPLAY"
    assert result.info.latest_clock is not None
    assert result.info.latest_clock.utc == CT
    assert not any("warning" in entry for entry in served.log)


def test_the_file_is_sent_unchanged_after_the_header(tmp_path):
    data = np.random.default_rng(2).integers(0, 256, size=2 * 600, dtype=np.uint8).tobytes()
    path = tmp_path / "noise.cu8"
    path.write_bytes(data)
    with _Server(path, "cu8", rate=20_000, chunk_samples=100) as served:
        received = _receive_all(served.server.port)
        served.wait_for("end of file")
    assert received[:12] == b"RTL0" + struct.pack(">II", 5, 29)
    assert received[12:] == data


def test_cf32_becomes_rtl_tcp_bytes_and_clipping_is_reported():
    log: list[str] = []

    iq = np.array([0, 1, -1 + 0.5j, 2 - 2j], dtype=np.complex64)
    source = replay.IqSource.__new__(replay.IqSource)
    source.format, source._log, source._clip_warned = "cf32", log.append, False
    source._data, source.n_samples = iq, len(iq)
    assert list(source.read(0, 4)) == [128, 128, 255, 128, 0, 191, 255, 0]
    assert len(log) == 1 and "clipped" in log[0]
    assert list(source.read(3, 10)) == [255, 0]
    assert len(log) == 1


def test_commands_are_logged_and_a_different_rate_is_reported(tmp_path):
    path = tmp_path / "quiet.cu8"
    path.write_bytes(bytes([127, 128]) * 1000)
    with _Server(path, "cu8", rate=FS, loop=True) as served:
        with RtlTcpClient("127.0.0.1", served.server.port) as client:
            client.configure_fm(1_024_000, gain_db=19.7)
            client.set_frequency(102_400_000)
            served.wait_for("client: set frequency 102.400 MHz")
        warning = served.wait_for("warning: the client asked for 1024000 S/s")
        served.wait_for("client: set gain 19.7 dB")
    assert f"serving it at {FS} S/s, unchanged" in warning


def test_clients_are_served_one_after_another_from_the_start(tmp_path):
    data = bytes(range(200))
    path = tmp_path / "ramp.cu8"
    path.write_bytes(data)
    with _Server(path, "cu8", rate=10_000, chunk_samples=50) as served:
        first = _receive_all(served.server.port)
        second = _receive_all(served.server.port)
    assert first[12:] == second[12:] == data


def test_a_slow_client_loses_old_samples_as_with_rtl_tcp(tmp_path):
    path = tmp_path / "ramp.cu8"
    path.write_bytes(bytes(range(256)) * 512)  # 65536 samples
    # Fast enough to fill the socket buffers while the client does not read.
    served = _Server(path, "cu8", rate=4_000_000, max_queue_s=0.2, loop=True)
    with served, socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.connect(("127.0.0.1", served.server.port))
        warning = served.wait_for("warning: the client fell", timeout_s=5.0)
    assert "as a full rtl_tcp buffer would" in warning


class TestMain:
    def test_the_format_must_be_known(self, tmp_path, capsys):
        path = tmp_path / "capture.bin"
        path.write_bytes(b"\x80" * 8)
        with pytest.raises(SystemExit) as exit_info:
            replay.main([str(path), "--port", "0"])
        assert exit_info.value.code == 2
        assert "--format cf32 or --format cu8" in capsys.readouterr().err

    def test_an_empty_file_is_refused(self, tmp_path, capsys):
        path = tmp_path / "empty.cu8"
        path.write_bytes(b"\x80")
        assert replay.main([str(path), "--port", "0"]) == 1
        assert "holds no IQ samples" in capsys.readouterr().err

    @pytest.mark.parametrize("argv", [["--rate", "0"], ["--port", "70000"], ["--format", "s16"]])
    def test_unusable_options_are_refused(self, tmp_path, argv):
        with pytest.raises(SystemExit) as exit_info:
            replay.main([str(tmp_path / "x.cu8"), *argv])
        assert exit_info.value.code == 2

    def test_a_queue_shorter_than_one_transfer_is_refused(self, tmp_path, capsys):
        # 0.01 s at 250 kS/s is 2500 samples: a 16384-sample transfer never fills.
        with pytest.raises(SystemExit) as exit_info:
            replay.main([str(tmp_path / "x.cu8"), "--rate", "250000", "--max-queue-s", "0.01"])
        assert exit_info.value.code == 2
        error = capsys.readouterr().err
        assert "--max-queue-s 0.01 holds less than one transfer of 16384 samples" in error
        assert "use at least 0.065536 s" in error

    def test_the_server_needs_a_queue_of_one_transfer(self, tmp_path):
        path = tmp_path / "ramp.cu8"
        path.write_bytes(bytes(range(200)))
        source = replay.IqSource(path, "cu8")
        with pytest.raises(ValueError, match="less than one transfer of 50 samples"):
            replay.ReplayServer(source, port=0, rate=10_000, chunk_samples=50, max_queue_s=0.0049)
        with _Server(path, "cu8", rate=10_000, chunk_samples=50, max_queue_s=0.005) as served:
            assert served.server.max_queue_samples == served.server.chunk_samples == 50

    def test_formats_follow_the_suffix(self, tmp_path):
        assert replay.guess_format(tmp_path / "a.CF32") == "cf32"
        assert replay.guess_format(tmp_path / "a.cu8") == "cu8"
        assert replay.guess_format(tmp_path / "a.wav") is None
