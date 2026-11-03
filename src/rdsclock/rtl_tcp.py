"""Minimal context-manager client for the ``rtl_tcp`` daemon."""

import math
import select
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
CMD_SET_TEST_MODE = 0x07
CMD_SET_AGC_MODE = 0x08
CMD_SET_DIRECT_SAMPLING = 0x09

# rtl_tcp forwards librtlsdr's default 256 KiB transfers, each once its last
# sample exists: 55 ms at 2.4 MS/s, 0.52 s at 250 kS/s.
RTL_TCP_TRANSFER_SAMPLES = 131_072


@dataclass
class DongleInfo:
    magic: bytes
    tuner_type: int
    gain_count: int


@dataclass(frozen=True)
class FreshCapture:
    """IQ collected after discarding queued data and allowing tuner settling.

    ``capture_start_monotonic_ns`` dates the first sample on the host
    monotonic clock; sample ``i`` follows ``i / sample_rate`` later. It is the
    time before the first capture read, unless the reads prove the samples
    older: they are produced in real time, so once ``k`` of them have arrived
    at ``t``, the first existed by ``t - k / sample_rate``, and the earliest
    such bound wins. ``queued_s`` is how far before the read that puts the
    first sample. Up to one rtl_tcp transfer is normal, since rtl_tcp sends a
    transfer once its last sample exists; ``backlogged`` marks a capture that
    started further behind, with samples that had queued while the client was
    not reading.

    The earliest bound dates the first sample only once the stream has caught
    up with real time. A backlog longer than the capture can deliver all of it
    ahead of real time, and the samples are then older than the bound by an
    unknown amount. ``caught_up`` is false for such a capture: it began behind
    and its reads were still moving the start back near the end.

    Samples lost during a capture only loosen the later bounds. The start
    stays right for the samples before the loss, and later samples are early
    by the time lost. ``capture_end_monotonic_ns``, the time after the last
    read, shows such a loss: the capture then spans more than
    ``len(iq) / sample_rate`` from its start, by the lost time plus up to one
    transfer.

    RTL-TCP provides neither hardware timestamps nor a tuning acknowledgement,
    so this is not a hardware time reference.
    """

    iq: np.ndarray
    capture_start_monotonic_ns: int
    discarded_samples: int
    capture_end_monotonic_ns: int | None = None
    queued_s: float = 0.0
    backlogged: bool = False
    caught_up: bool = True


class RtlTcpDrainTimeoutError(TimeoutError):
    """No fresh capture boundary could be established within the deadline."""


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
        try:
            s.settimeout(self.read_timeout)
            header = self._recv_exact(s, 12)
            magic = header[:4]
            if magic != b"RTL0":
                raise OSError(f"unexpected magic: {magic!r}")
            tuner = struct.unpack(">I", header[4:8])[0]
            gain_count = struct.unpack(">I", header[8:12])[0]
        except BaseException:
            s.close()
            raise
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

    def set_digital_agc(self, enabled: bool) -> None:
        """Set RTL2832 digital AGC independently of the tuner's gain mode."""
        self._send_cmd(CMD_SET_AGC_MODE, int(bool(enabled)))

    def set_direct_sampling(self, mode: int = 0) -> None:
        """Select normal tuner input (0), direct I (1), or direct Q (2)."""
        if mode not in (0, 1, 2):
            raise ValueError("direct sampling mode must be 0, 1, or 2")
        self._send_cmd(CMD_SET_DIRECT_SAMPLING, mode)

    def set_test_mode(self, enabled: bool) -> None:
        """Enable or disable the device's synthetic test pattern."""
        self._send_cmd(CMD_SET_TEST_MODE, int(bool(enabled)))

    def configure_fm(self, sample_rate: int, gain_db: float | None = None, ppm: int = 0) -> None:
        """Apply a repeatable FM profile without changing bias-tee power.

        Digital AGC, direct sampling and the test pattern are disabled. PPM
        is always sent, including zero, so a previous client's correction
        cannot silently carry over. ``gain_db=None`` selects tuner AGC.
        """
        if not math.isfinite(sample_rate) or sample_rate <= 0 or int(sample_rate) != sample_rate:
            raise ValueError("sample_rate must be a positive integer")
        if gain_db is not None and not math.isfinite(gain_db):
            raise ValueError("gain_db must be finite")
        if not math.isfinite(ppm) or int(ppm) != ppm:
            raise ValueError("ppm must be a finite integer")
        self.set_direct_sampling(0)
        self.set_test_mode(False)
        self.set_digital_agc(False)
        self.set_ppm(ppm)
        self.set_sample_rate(sample_rate)
        if gain_db is None:
            self.set_gain_mode_auto()
        else:
            self.set_gain_mode_manual(round(gain_db * 10))

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
        return self._iq_from_bytes(self._recv_exact(self._sock, n_samples * 2))

    @staticmethod
    def _iq_from_bytes(data: bytes | bytearray) -> np.ndarray:
        raw = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
        raw = (raw - 127.5) / 127.5
        iq = raw[0::2] + 1j * raw[1::2]
        return iq.astype(np.complex64)

    def record(self, duration_s: float, sample_rate: int) -> np.ndarray:
        """Record ``duration_s`` seconds and return a complex64 ndarray."""
        n = int(round(duration_s * sample_rate))
        return self.read_iq(n)

    def capture_fresh(
        self,
        duration_s: float,
        sample_rate: int,
        *,
        settle_s: float = 0.3,
        max_drain_s: float | None = None,
        quiet_s: float | None = None,
        backlog_threshold_s: float | None = None,
    ) -> FreshCapture:
        """Discard stale IQ after a retune, then collect a full capture.

        Call after changing frequency, gain, or sample rate. Unlike the legacy
        ``read_iq(settle_s=...)`` sleep, settling here actively consumes data.
        Once settling has elapsed, the socket must remain empty for a short
        interval before collection begins. That interval defaults to 1024
        sample periods (capped at 50 ms), allowing high-rate streams to use
        the gaps between their shorter bursts.

        A bounded drain raises ``RtlTcpDrainTimeoutError`` if no such boundary can
        be established. The default deadline allows at least two seconds
        beyond settling. There are no tune markers or timestamps in RTL-TCP:
        an empty client socket cannot prove that a delayed remote buffer is
        fresh. Use a continuously drained receiver and verify station PI when
        strict frequency attribution is needed.

        The quiet interval can also fall between two bursts of a backlog: while
        the client decodes instead of reading, rtl_tcp queues the samples and
        then sends them faster than real time. The capture start is therefore
        dated by when the samples arrived (see :class:`FreshCapture`), which
        keeps receive times consistent with the samples. It does not make the
        samples fresh: after a retune, a backlog can begin with the previous
        tuning. ``backlogged`` is set when the first sample had queued for
        longer than ``backlog_threshold_s``; the default allows two rtl_tcp
        transfers, and at least 0.25 s. A backlogged capture has caught up
        once, for the last four thresholds of the read (1 s at 2.4 MS/s), no
        read moved its start back by more than a fifth of a threshold. That
        recognises a stream that runs more than 5 % ahead of real time; a
        link barely faster than the stream can still hide a backlog.

        Any failure or interruption after draining starts closes the socket:
        a partial read can otherwise leave the next attempt between I and Q.
        Reconnect before retrying a failed capture. Invalid arguments are
        rejected before I/O and leave an existing connection untouched.
        """
        if self._sock is None:
            raise RuntimeError("not connected")
        if not math.isfinite(duration_s) or duration_s <= 0:
            raise ValueError("duration_s must be finite and positive")
        if not math.isfinite(sample_rate) or sample_rate <= 0:
            raise ValueError("sample_rate must be finite and positive")
        if not math.isfinite(settle_s) or settle_s < 0:
            raise ValueError("settle_s must be finite and nonnegative")
        if quiet_s is None:
            quiet_s = min(0.05, 1024 / sample_rate)
        if max_drain_s is None:
            max_drain_s = max(5.0, settle_s + 2.0)
        if not math.isfinite(quiet_s) or quiet_s <= 0:
            raise ValueError("quiet_s must be finite and positive")
        if not math.isfinite(max_drain_s) or max_drain_s <= max(settle_s, quiet_s):
            raise ValueError("max_drain_s must exceed settle_s and quiet_s")
        if backlog_threshold_s is None:
            backlog_threshold_s = max(0.25, 2 * RTL_TCP_TRANSFER_SAMPLES / sample_rate)
        if not math.isfinite(backlog_threshold_s) or backlog_threshold_s < 0:
            raise ValueError("backlog_threshold_s must be finite and nonnegative")
        n_samples = int(round(duration_s * sample_rate))
        if n_samples < 1:
            raise ValueError("capture must contain at least one sample")

        try:
            discarded = self._discard_until_quiet(settle_s, max_drain_s, quiet_s)
            return self._read_capture(n_samples, sample_rate, discarded // 2, backlog_threshold_s)
        except BaseException:
            self.close()
            raise

    def _read_capture(
        self,
        n_samples: int,
        sample_rate: float,
        discarded_samples: int,
        backlog_threshold_s: float,
    ) -> FreshCapture:
        """Read ``n_samples`` and date the first one by when the samples arrived."""
        sock = self._sock
        if sock is None:
            raise RuntimeError("not connected")
        # A read that moves the start back by more than ``step_ns`` shows the
        # stream still running ahead of real time; ``window_ns`` without one
        # shows that it has caught up.
        step_ns = round(backlog_threshold_s / 5 * 1e9)
        window_ns = round(4 * backlog_threshold_s * 1e9)
        data = bytearray(2 * n_samples)
        view = memoryview(data)
        received = 0
        read_start_ns = start_ns = level_ns = end_ns = time.monotonic_ns()
        ran_ahead_ns: int | None = None
        while received < len(data):
            count = sock.recv_into(view[received:])
            if not count:
                raise OSError("rtl_tcp: socket closed")
            received += count
            end_ns = time.monotonic_ns()
            # k samples take k / sample_rate to produce: the first existed that long ago.
            bound_ns = end_ns - round(received // 2 * 1e9 / sample_rate)
            start_ns = min(start_ns, bound_ns)
            if bound_ns < level_ns - step_ns:
                level_ns, ran_ahead_ns = bound_ns, end_ns
        queued_s = (read_start_ns - start_ns) / 1e9
        backlogged = queued_s > backlog_threshold_s
        still_ahead = ran_ahead_ns is not None and end_ns - ran_ahead_ns < window_ns
        return FreshCapture(
            self._iq_from_bytes(data),
            start_ns,
            discarded_samples,
            capture_end_monotonic_ns=end_ns,
            queued_s=queued_s,
            backlogged=backlogged,
            caught_up=not (backlogged and still_ahead),
        )

    def _discard_until_quiet(self, settle_s: float, max_drain_s: float, quiet_s: float) -> int:
        sock = self._sock
        if sock is None:
            raise RuntimeError("not connected")
        started = time.monotonic()
        deadline = started + max_drain_s
        settled_at = started + settle_s
        last_data_at = started
        discarded = 0
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise RtlTcpDrainTimeoutError(
                    "rtl_tcp: no fresh capture boundary within "
                    f"{max_drain_s:g}s ({discarded // 2} samples discarded); "
                    "the stream did not become quiet or ended on a partial IQ sample"
                )
            ready_at = max(settled_at, last_data_at + quiet_s)
            if now >= ready_at and discarded % 2 == 0:
                # Check once more instead of declaring success while bytes
                # that arrived at the end of the quiet interval are pending.
                readable, _, _ = select.select([sock], [], [], 0)
                if not readable:
                    return discarded
            else:
                wait_until = ready_at if now < ready_at else deadline
                readable, _, _ = select.select([sock], [], [], min(wait_until, deadline) - now)
                if not readable:
                    continue
            # TCP may split an IQ pair across reads. Never start a capture
            # with the Q byte of a discarded sample.
            chunk = sock.recv(65_536)
            if not chunk:
                raise OSError("rtl_tcp: socket closed while discarding stale IQ")
            discarded += len(chunk)
            last_data_at = time.monotonic()
