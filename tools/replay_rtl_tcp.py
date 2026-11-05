#!/usr/bin/env python3
"""Serve a recorded IQ file as an ``rtl_tcp`` receiver: a test aid, not a receiver.

    python tools/replay_rtl_tcp.py FILE --rate 250000 --format cf32 --port 1235 [--loop]

Any ``rtl_tcp`` client, ``rdsclock recon`` included, can then connect to
127.0.0.1:1235 as if a dongle were there. The server speaks the rtl_tcp
protocol: a 12-byte header (``RTL0``, tuner type, gain count), then the
file's IQ as interleaved unsigned bytes at the file's real-time rate.

What it does NOT do:

* It replays an OLD broadcast. The RDS Clock-Time in the file is as old as
  the recording: next to live stations it is stale, and the consensus will
  rightly treat it as an outlier. Replays of the same recording session
  agree with each other, so a consensus of replays alone reports the
  recording's time, not the current time. Use it to exercise decoding,
  multi-receiver runs and dashboards, never as a time source.
* It never changes the file's content. Client commands (frequency, sample
  rate, gain, ...) are accepted and logged, but every tuning receives the
  same recording. A requested sample rate that differs from ``--rate`` is
  reported and ignored: the file is served at its own rate.
* It serves one client at a time; further clients wait in the backlog.
  Each client starts at the beginning of the file. Without ``--loop`` the
  connection closes at the end of the file.

``cf32`` files (little-endian complex64, as written by ``rdsclock`` and
``dsp.write_iq_complex64``) are converted to bytes as rtl_tcp sends them,
``round(x * 127.5 + 127.5)``; values beyond ±1 are clipped, with a warning.
``cu8`` files (rtl_sdr's format) are sent byte for byte.

Like rtl_tcp, it keeps producing samples while the client does not read,
then sends the queued samples as fast as the client takes them. A queue
longer than ``--max-queue-s`` is dropped, as a full rtl_tcp buffer would be.
The queue must hold at least one transfer (``--chunk-samples``): a shorter
one could never send a sample, so it is refused.

Only the standard library and numpy are used. It listens on 127.0.0.1 by
default; ``--host`` changes that, but it has no authentication.
"""

from __future__ import annotations

import argparse
import contextlib
import select
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

DEFAULT_RATE = 250_000
DEFAULT_PORT = 1235
DEFAULT_CHUNK_SAMPLES = 16_384  # 66 ms at 250 kS/s: quiet gaps between bursts, like rtl_tcp
DEFAULT_MAX_QUEUE_S = 20.0
TUNER_R820T = 5
R820T_GAIN_COUNT = 29

FORMATS = ("cf32", "cu8")
_SUFFIX_FORMATS = {
    ".cf32": "cf32",
    ".fc32": "cf32",
    ".c64": "cf32",
    ".complex64": "cf32",
    ".cu8": "cu8",
    ".u8": "cu8",
}

COMMAND_NAMES = {
    0x01: "set frequency",
    0x02: "set sample rate",
    0x03: "set gain mode",
    0x04: "set gain",
    0x05: "set frequency correction",
    0x06: "set IF gain",
    0x07: "set test mode",
    0x08: "set AGC mode",
    0x09: "set direct sampling",
    0x0A: "set offset tuning",
    0x0B: "set RTL crystal",
    0x0C: "set tuner crystal",
    0x0D: "set gain by index",
    0x0E: "set bias tee",
}

Log = Callable[[str], None]


def _stderr_log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} replay: {message}", file=sys.stderr, flush=True)


def guess_format(path: Path) -> str | None:
    """The IQ format that the file name suggests, or ``None``."""
    return _SUFFIX_FORMATS.get(path.suffix.lower())


class IqSource:
    """The file's IQ as rtl_tcp bytes, read in pieces so that long files stay on disk."""

    def __init__(self, path: Path, fmt: str, log: Log = _stderr_log) -> None:
        if fmt not in FORMATS:
            raise ValueError(f"format must be one of {', '.join(FORMATS)}")
        self.format = fmt
        self._log = log
        self._clip_warned = False
        dtype = np.complex64 if fmt == "cf32" else np.uint8
        self._data = np.memmap(path, dtype=dtype, mode="r")
        # A cu8 sample is an (I, Q) pair; an odd trailing byte is ignored.
        self.n_samples = len(self._data) if fmt == "cf32" else len(self._data) // 2
        if self.n_samples == 0:
            raise ValueError(f"{path} holds no IQ samples")

    def read(self, start: int, count: int) -> bytes:
        """``count`` samples from ``start`` (at most to the end of the file) as u8 IQ."""
        stop = min(start + count, self.n_samples)
        if self.format == "cu8":
            return self._data[2 * start : 2 * stop].tobytes()
        iq = np.asarray(self._data[start:stop])
        out = np.empty(2 * len(iq), dtype=np.float32)
        out[0::2], out[1::2] = iq.real, iq.imag
        scaled = np.rint(out * 127.5 + 127.5)
        clipped = (scaled < 0) | (scaled > 255)
        if clipped.any() and not self._clip_warned:
            self._clip_warned = True
            self._log("warning: the file has values beyond ±1; they are clipped to 0..255")
        return np.clip(scaled, 0, 255).astype(np.uint8).tobytes()


class ReplayServer:
    """An rtl_tcp server that streams one :class:`IqSource` to each client in turn.

    Bind with ``port=0`` for a free port; :attr:`port` tells which.
    :meth:`serve_forever` returns after :meth:`shutdown`.
    """

    def __init__(
        self,
        source: IqSource,
        rate: int = DEFAULT_RATE,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        loop: bool = False,
        chunk_samples: int = DEFAULT_CHUNK_SAMPLES,
        max_queue_s: float = DEFAULT_MAX_QUEUE_S,
        tuner_type: int = TUNER_R820T,
        gain_count: int = R820T_GAIN_COUNT,
        log: Log = _stderr_log,
    ) -> None:
        if rate <= 0 or chunk_samples <= 0 or max_queue_s <= 0:
            raise ValueError("rate, chunk_samples and max_queue_s must be positive")
        problem = queue_problem(max_queue_s, rate, chunk_samples)
        if problem:
            raise ValueError(problem)
        self.source = source
        self.rate = rate
        self.loop = loop
        self.chunk_samples = chunk_samples
        self.max_queue_samples = round(max_queue_s * rate)
        self.header = b"RTL0" + struct.pack(">II", tuner_type, gain_count)
        self.log = log
        self._stop = threading.Event()
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self._listener = socket.create_server((host, port), family=family, backlog=1)
        self.host = host
        self.port = self._listener.getsockname()[1]

    def shutdown(self) -> None:
        self._stop.set()

    def serve_forever(self) -> None:
        self.log(
            f"listening on {self.host}:{self.port}: {self.source.n_samples / self.rate:.1f} s "
            f"of {self.source.format} at {self.rate} S/s{', looped' if self.loop else ''}"
        )
        try:
            while not self._stop.is_set():
                readable, _, _ = select.select([self._listener], [], [], 0.2)
                if not readable:
                    continue
                conn, peer = self._listener.accept()
                with conn:
                    self._serve_client(conn, peer)
        finally:
            self._listener.close()

    def _serve_client(self, conn: socket.socket, peer: tuple) -> None:
        self.log(f"client {peer[0]}:{peer[1]} connected")
        commands = bytearray()
        pending = memoryview(b"")
        offset = 0  # next file sample to send
        sent = 0  # samples of this session queued or sent, dropped ones included
        started = time.monotonic()
        try:
            conn.sendall(self.header)
            conn.setblocking(False)
            while not self._stop.is_set():
                # Samples that "exist" by now but have not been queued for the client.
                behind = int((time.monotonic() - started) * self.rate) - sent
                if behind > self.max_queue_samples:
                    dropped = behind - min(self.chunk_samples, self.max_queue_samples)
                    self.log(
                        f"warning: the client fell {behind / self.rate:.1f} s behind; "
                        f"dropping {dropped / self.rate:.1f} s, as a full rtl_tcp buffer would"
                    )
                    offset, sent = self._advance(offset, dropped), sent + dropped
                    behind -= dropped
                if not pending:
                    if offset >= self.source.n_samples:
                        if not self.loop:
                            self.log("end of file: closing the connection")
                            return
                        offset = 0
                    # Like rtl_tcp, send a transfer once its last sample exists.
                    if behind >= self.chunk_samples:
                        data = self.source.read(offset, self.chunk_samples)
                        count = len(data) // 2
                        offset, sent = offset + count, sent + count
                        pending = memoryview(data)
                if pending:
                    timeout = 0.2  # select wakes as soon as the client takes more
                else:
                    due = started + (sent + self.chunk_samples) / self.rate
                    timeout = max(0.0, min(due - time.monotonic(), 0.2))
                readable, writable, _ = select.select(
                    [conn], [conn] if pending else [], [], timeout
                )
                if readable:
                    chunk = conn.recv(4096)
                    if not chunk:
                        self.log("client disconnected")
                        return
                    commands.extend(chunk)
                    while len(commands) >= 5:
                        command, value = struct.unpack(">BI", commands[:5])
                        del commands[:5]
                        self._log_command(command, value)
                if writable and pending:
                    pending = pending[conn.send(pending) :]
        except (BrokenPipeError, ConnectionResetError):
            # A client that closes with IQ still unread resets the connection.
            self.log("client disconnected")
        except OSError as exc:
            self.log(f"client connection ended: {exc}")

    def _advance(self, offset: int, count: int) -> int:
        """The file position ``count`` samples after ``offset``, wrapping when looped."""
        if self.loop:
            return (offset + count) % self.source.n_samples
        return min(offset + count, self.source.n_samples)

    def _log_command(self, command: int, value: int) -> None:
        name = COMMAND_NAMES.get(command, f"command 0x{command:02X}")
        if command == 0x01:
            detail = f"{value / 1e6:.3f} MHz (the file is served unchanged)"
        elif command == 0x02:
            detail = f"{value} S/s"
            if value != self.rate:
                self.log(
                    f"warning: the client asked for {value} S/s, but the file is "
                    f"{self.rate} S/s; serving it at {self.rate} S/s, unchanged"
                )
        elif command == 0x03:
            detail = "manual" if value else "automatic"
        elif command == 0x04:
            detail = f"{value / 10:.1f} dB"
        else:
            detail = str(value)
        self.log(f"client: {name} {detail}")


def queue_problem(max_queue_s: float, rate: int, chunk_samples: int) -> str | None:
    """Why ``max_queue_s`` cannot work at ``rate`` with these transfers, or ``None``.

    A transfer leaves once all its samples exist; a queue limit below one
    transfer drops them before that, so nothing would ever be sent.
    """
    if round(max_queue_s * rate) >= chunk_samples:
        return None
    return (
        f"--max-queue-s {max_queue_s:g} holds less than one transfer of {chunk_samples} "
        f"samples at {rate} S/s; use at least {chunk_samples / rate:g} s "
        "or fewer --chunk-samples"
    )


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{text} is not a positive integer")
    return value


def _port(text: str) -> int:
    value = int(text)
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"{text} is not a TCP port (0-65535)")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve a recorded IQ file as an rtl_tcp receiver (a test aid: the "
        "recording's Clock-Time is old, so a live consensus treats it as an outlier).",
    )
    parser.add_argument("file", type=Path, help="IQ recording")
    parser.add_argument(
        "--rate",
        type=_positive_int,
        default=DEFAULT_RATE,
        help=f"the file's sample rate in S/s (default {DEFAULT_RATE})",
    )
    parser.add_argument(
        "--format",
        choices=FORMATS,
        default=None,
        help="cf32: little-endian complex64; cu8: interleaved unsigned bytes "
        "(default: from the file name's suffix)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="listen address (default 127.0.0.1)")
    parser.add_argument(
        "--port", type=_port, default=DEFAULT_PORT, help=f"(default {DEFAULT_PORT}; 0: any)"
    )
    parser.add_argument("--loop", action="store_true", help="start again at the end of the file")
    parser.add_argument(
        "--chunk-samples",
        type=_positive_int,
        default=DEFAULT_CHUNK_SAMPLES,
        help=f"samples per transfer (default {DEFAULT_CHUNK_SAMPLES})",
    )
    parser.add_argument(
        "--max-queue-s",
        type=float,
        default=DEFAULT_MAX_QUEUE_S,
        help=(
            f"drop samples queued for longer than this (default {DEFAULT_MAX_QUEUE_S:g} s); "
            "it must hold at least one transfer"
        ),
    )
    parser.add_argument("--tuner-type", type=int, default=TUNER_R820T, help="header tuner type")
    parser.add_argument("--gain-count", type=int, default=R820T_GAIN_COUNT, help="header gains")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    fmt = args.format or guess_format(args.file)
    if fmt is None:
        parser.error(f"cannot tell the format of {args.file}: add --format cf32 or --format cu8")
    if not args.max_queue_s > 0:
        parser.error("--max-queue-s must be positive")
    problem = queue_problem(args.max_queue_s, args.rate, args.chunk_samples)
    if problem:
        parser.error(problem)
    try:
        source = IqSource(args.file, fmt)
        server = ReplayServer(
            source,
            rate=args.rate,
            host=args.host,
            port=args.port,
            loop=args.loop,
            chunk_samples=args.chunk_samples,
            max_queue_s=args.max_queue_s,
            tuner_type=args.tuner_type,
            gain_count=args.gain_count,
        )
    except (OSError, ValueError) as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 1
    _stderr_log(
        "a test aid: this replays an old broadcast, so its Clock-Time is stale "
        "and a live consensus will mark it as an outlier"
    )
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
