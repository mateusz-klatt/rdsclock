"""Minimal context-manager client for the ``rtl_tcp`` daemon."""

import socket
import struct
import time
from dataclasses import dataclass

import numpy as np

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 1234

# rtl_tcp protocol commands (subset — see librtlsdr/src/rtl_tcp.c for the full list).
CMD_SET_FREQUENCY = 0x01
CMD_SET_SAMPLE_RATE = 0x02
CMD_SET_GAIN_MODE = 0x03  # selects AGC versus manual gain
CMD_SET_GAIN = 0x04  # tenths of a dB
CMD_SET_FREQ_CORRECTION = 0x05  # ppm
CMD_SET_AGC_MODE = 0x08
CMD_SET_DIRECT_SAMPLING = 0x09


@dataclass
class DongleInfo:
    magic: bytes
    tuner_type: int
    gain_count: int


class RtlTcpClient:
    """Thin TCP wrapper. Every command is a 5-byte big-endian record."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        connect_timeout: float = 10.0,
        read_timeout: float = 15.0,
    ):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._sock: socket.socket | None = None
        self.info: DongleInfo | None = None

    # ---- context manager ----
    def __enter__(self) -> "RtlTcpClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- connection lifecycle ----
    def connect(self) -> DongleInfo:
        if self._sock is not None:
            return self.info  # type: ignore[return-value]
        s = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        s.settimeout(self.read_timeout)
        header = self._recv_exact(s, 12)
        magic = header[:4]
        if magic != b"RTL0":
            s.close()
            raise OSError(f"unexpected magic: {magic!r}")
        tuner = struct.unpack(">I", header[4:8])[0]
        gain_count = struct.unpack(">I", header[8:12])[0]
        self._sock = s
        self.info = DongleInfo(magic=magic, tuner_type=tuner, gain_count=gain_count)
        return self.info

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    # ---- commands ----
    def _send_cmd(self, cmd: int, value: int) -> None:
        if self._sock is None:
            raise RuntimeError("not connected — call connect() or use a with-statement")
        self._sock.sendall(struct.pack(">BI", cmd, int(value) & 0xFFFFFFFF))

    def set_frequency(self, freq_hz: int) -> None:
        self._send_cmd(CMD_SET_FREQUENCY, int(freq_hz))

    def set_sample_rate(self, rate: int) -> None:
        self._send_cmd(CMD_SET_SAMPLE_RATE, int(rate))

    def set_gain_mode_auto(self) -> None:
        self._send_cmd(CMD_SET_GAIN_MODE, 0)

    def set_gain_mode_manual(self, gain_tenth_db: int) -> None:
        self._send_cmd(CMD_SET_GAIN_MODE, 1)
        self._send_cmd(CMD_SET_GAIN, int(gain_tenth_db))

    def set_ppm(self, ppm: int) -> None:
        self._send_cmd(CMD_SET_FREQ_CORRECTION, int(ppm))

    # ---- sample reception ----
    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("rtl_tcp: socket closed")
            buf.extend(chunk)
        return bytes(buf)

    def read_iq(self, n_samples: int, settle_s: float = 0.2) -> np.ndarray:
        """Fetch ``n_samples`` IQ samples (each = 2 bytes uint8) as complex64."""
        if self._sock is None:
            raise RuntimeError("not connected")
        if settle_s > 0:
            time.sleep(settle_s)
        n_bytes = n_samples * 2
        data = self._recv_exact(self._sock, n_bytes)
        raw = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
        raw = (raw - 127.5) / 127.5
        iq = raw[0::2] + 1j * raw[1::2]
        return iq.astype(np.complex64)

    def record(self, duration_s: float, sample_rate: int) -> np.ndarray:
        """Record ``duration_s`` seconds and return a complex64 ndarray."""
        n = int(round(duration_s * sample_rate))
        return self.read_iq(n)
