#!/usr/bin/env python3
"""Empirically measure RTL-SDR settling time after retune.

Captures a continuous IQ stream that retunes between a strong station
and an empty channel, then plots 19 kHz pilot power per ~100 ms window.
The pilot is a deterministic FM-broadcast tone — its rise/fall after
a retune is a direct measurement of PLL + AGC + RDS-pipeline settling.

Usage:
    .venv/bin/python tools/measure_settle.py --strong 91.0 --empty 89.5

Output:
    eter/settle-measure-YYYYMMDD-HHMMSS/
        capture.iq                  continuous IQ (interleaved tunes)
        pilot-power-vs-time.csv     per-window pilot power
        settle.png                  plot if matplotlib available
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import socket
import struct
import sys
from pathlib import Path

import numpy as np
from scipy.signal import firwin, lfilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rdsclock import dsp  # noqa: E402

FS = 250_000  # rtl_tcp sample rate
PILOT_HZ = 19_000
PILOT_HALF_BW_HZ = 500  # narrow bandpass around 19 kHz
WINDOW_SEC = 0.1  # power measurement granularity


def _rtl_tcp_send_cmd(sock: socket.socket, cmd: int, value: int) -> None:
    """Send rtl_tcp protocol command (1 byte cmd + 4 big-endian uint32 value)."""
    sock.sendall(struct.pack(">BI", cmd, value))


def set_frequency(sock: socket.socket, freq_hz: int) -> None:
    _rtl_tcp_send_cmd(sock, 0x01, freq_hz)


def set_sample_rate(sock: socket.socket, fs: int) -> None:
    _rtl_tcp_send_cmd(sock, 0x02, fs)


def set_gain_mode_auto(sock: socket.socket) -> None:
    _rtl_tcp_send_cmd(sock, 0x03, 0)


def read_iq(sock: socket.socket, n_samples: int) -> np.ndarray:
    """Read N complex samples from rtl_tcp (u8 IQ interleaved → complex64)."""
    n_bytes = n_samples * 2
    buf = bytearray()
    while len(buf) < n_bytes:
        chunk = sock.recv(min(65536, n_bytes - len(buf)))
        if not chunk:
            break
        buf.extend(chunk)
    arr = np.frombuffer(bytes(buf), dtype=np.uint8)
    i = arr[0::2].astype(np.float32) - 127.5
    q = arr[1::2].astype(np.float32) - 127.5
    return ((i + 1j * q) / 127.5).astype(np.complex64)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strong", type=float, required=True, help="Strong-station freq MHz")
    p.add_argument("--empty", type=float, required=True, help="Empty-channel freq MHz")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1234)
    p.add_argument(
        "--phase-seconds",
        type=float,
        default=30.0,
        help="Capture seconds per phase (3 phases: strong → empty → strong)",
    )
    args = p.parse_args()

    ts = _dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "eter" / f"settle-measure-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    sock = socket.create_connection((args.host, args.port))
    print(f"connected to rtl_tcp {args.host}:{args.port}")
    # Skip rtl_tcp dongle-info header (12 bytes)
    sock.recv(12)
    set_sample_rate(sock, FS)
    set_gain_mode_auto(sock)

    phases = [
        ("strong-1", args.strong),
        ("empty", args.empty),
        ("strong-2", args.strong),
    ]
    samples_per_phase = int(args.phase_seconds * FS)
    all_iq = []
    phase_starts: list[tuple[str, int]] = []
    offset = 0

    for label, freq_mhz in phases:
        freq_hz = int(freq_mhz * 1e6)
        # Mark the retune moment *in the sample stream*. We record the
        # offset where we sent the tune command — the post-retune
        # settling window starts here.
        phase_starts.append((label, offset))
        print(f"phase {label}: tune to {freq_mhz:.2f} MHz @ sample {offset:,}")
        set_frequency(sock, freq_hz)
        iq = read_iq(sock, samples_per_phase)
        all_iq.append(iq)
        offset += len(iq)

    sock.close()
    full = np.concatenate(all_iq).astype(np.complex64)
    iq_path = out_dir / "capture.iq"
    full.tofile(iq_path)
    print(f"saved {len(full):,} samples → {iq_path}")

    # Bandpass around 19 kHz pilot using zero-phase FIR.
    bpf = firwin(
        numtaps=255,
        cutoff=[PILOT_HZ - PILOT_HALF_BW_HZ, PILOT_HZ + PILOT_HALF_BW_HZ],
        pass_zero=False,
        fs=FS,
    )
    # We work on the FM-demodulated baseband. FM demod needs ≥ 2 samples.
    fm = dsp.fm_demod(full)
    pilot_only = lfilter(bpf, 1.0, fm)

    # Per-window RMS power, in dB relative to peak.
    n_per_window = int(WINDOW_SEC * FS)
    n_windows = len(pilot_only) // n_per_window
    powers = np.empty(n_windows, dtype=np.float32)
    for i in range(n_windows):
        seg = pilot_only[i * n_per_window : (i + 1) * n_per_window]
        powers[i] = float(np.sqrt(np.mean(seg.astype(np.float32) ** 2)))
    peak = float(np.max(powers)) or 1.0
    powers_db = 20.0 * np.log10(np.maximum(powers, 1e-9) / peak)

    csv_path = out_dir / "pilot-power-vs-time.csv"
    with open(csv_path, "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["time_seconds", "pilot_rms", "pilot_rel_db", "phase"])
        # Convert sample-offset phase boundaries to window indices.
        phase_window = {win_idx: lbl for lbl, sample_idx in phase_starts
                        for win_idx in [sample_idx // n_per_window]}
        active_phase = phases[0][0]
        for i in range(n_windows):
            if i in phase_window:
                active_phase = phase_window[i]
            t_sec = i * WINDOW_SEC
            w.writerow([f"{t_sec:.2f}", f"{powers[i]:.6g}", f"{powers_db[i]:.2f}", active_phase])
    print(f"saved per-window pilot power → {csv_path}")

    # Settling estimate: per "strong" phase, find first window after retune
    # whose pilot is within −3 dB of the phase's median.
    print()
    print("Empirical settling time per phase:")
    for label, sample_idx in phase_starts:
        if not label.startswith("strong"):
            continue
        win_idx = sample_idx // n_per_window
        phase_end = win_idx + int(args.phase_seconds / WINDOW_SEC)
        phase_powers = powers_db[win_idx:phase_end]
        if len(phase_powers) == 0:
            continue
        # "Stable" defined as within 3 dB of the median of the last
        # half of the phase (assumed fully settled by then).
        stable_target = float(np.median(phase_powers[len(phase_powers) // 2 :])) - 3.0
        # Find first sample-window at or above target.
        idx = int(np.argmax(phase_powers >= stable_target))
        settle_sec = idx * WINDOW_SEC
        print(f"  {label}: pilot reaches median−3 dB at +{settle_sec:.2f} s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
