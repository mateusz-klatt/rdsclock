"""Overnight recorder: capture 4 FM stations, 60s each, every 30 minutes.

This script is useful when validating the receiver against changing
RF conditions over a long observation window. It writes one ``.iq``
file per station per cycle into ``./night/`` and appends a one-line
status entry to ``./night_log.txt``.

Run from the repository root with the editable install active:

    .venv/bin/python scripts/night_recorder.py
"""

import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from rdsclock import dsp
from rdsclock.decoder import decode_iq
from rdsclock.rtl_tcp import RtlTcpClient

FREQS = [89.0, 95.8, 102.4, 106.8]
DURATION_S = 60
SAMPLE_RATE = 250_000
GAIN_DB = 35
INTERVAL_S = 1800  # every 30 minutes
OUTPUT_DIR = Path(__file__).parent / "night"
OUTPUT_DIR.mkdir(exist_ok=True)
LOG_FILE = Path(__file__).parent / "night_log.txt"


def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def record_and_decode(freq_mhz: float, label: str) -> int:
    """Record one station, decode it, and return the number of groups found."""
    try:
        with RtlTcpClient(host="localhost", port=1234, connect_timeout=10) as client:
            client.set_sample_rate(SAMPLE_RATE)
            client.set_gain_mode_manual(GAIN_DB * 10)
            client.set_frequency(int(freq_mhz * 1e6))
            iq = client.record(DURATION_S, SAMPLE_RATE)
        power = 10 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-12)

        out_path = OUTPUT_DIR / f"{label}_{freq_mhz:06.2f}MHz.iq"
        dsp.write_iq_complex64(iq, str(out_path))

        result = decode_iq(iq, fs=SAMPLE_RATE)
        ps = result.info.ps_name
        ct = result.info.latest_clock
        ct_str = ct.utc.strftime("%H:%M") + "Z" if ct else "—"
        log(
            f"  {freq_mhz:6.2f} MHz: power={power:+5.1f}dBFS groups={result.n_groups:4d} "
            f"PS={ps!r:12s} CT={ct_str}  → {out_path.name}"
        )
        return result.n_groups
    except Exception as exc:
        log(f"  {freq_mhz:6.2f} MHz: ERROR {type(exc).__name__}: {exc}")
        return -1


def main() -> None:
    log("=" * 70)
    log(f"NIGHT RECORDER start, PID={os.getpid()}")
    log(f"  freqs: {FREQS}")
    log(f"  duration each: {DURATION_S}s, interval: {INTERVAL_S}s")
    log(f"  output: {OUTPUT_DIR}")
    log("=" * 70)

    cycle = 0
    while cycle < 16:  # max 16 cycles (8 hours at 30 minutes)
        cycle += 1
        label = f"c{cycle:02d}_{datetime.now().strftime('%H%M')}"
        log(f"Cycle {cycle} ({label}):")
        for freq in FREQS:
            record_and_decode(freq, label)
        log(f"  → sleep {INTERVAL_S}s")
        time.sleep(INTERVAL_S)

    log(f"NIGHT RECORDER done, {cycle} cycles")


if __name__ == "__main__":
    main()
