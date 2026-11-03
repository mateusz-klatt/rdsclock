"""Deterministic synthetic observations: source isolation and outlier rejection.

Run from a checkout: PYTHONPATH=src .venv/bin/python examples/consensus_demo.py
This exercises the consensus API; it does not receive or decode radio samples.
"""

from datetime import UTC, datetime, timedelta

from rdsclock.time_consensus import StationObservation, TimeConsensus


def main() -> int:
    base = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    consensus = TimeConsensus()
    sources = [
        (92.0, 0x3001, 0),
        (98.3, 0x3002, 2),
        (106.8, 0x3003, -2),
        (107.5, 0x3004, 600),
        (92.1, 0x3001, 0),  # a second frequency carrying the first PI
    ]
    print("SYNTHETIC observations: three agreeing PIs, one outlier, one repeated PI")
    for frequency, pi, offset in sources:
        consensus.record(
            StationObservation(
                freq_hz=frequency * 1e6,
                pi=pi,
                ct_utc=base + timedelta(seconds=offset),
                received_monotonic=100.0,
            )
        )
    result = consensus.consensus(monotonic_now=101.0)
    print(result.format_display())
    print(f"Contributing frequencies: {result.contributing_freqs_mhz}")
    print(f"Outliers: {result.outlier_freqs_mhz}")
    print("HIGH describes source agreement; shared errors and spoofing remain possible.")
    return 0 if result.n_sources == 3 and result.outlier_freqs_mhz == [107.5] else 1


if __name__ == "__main__":
    raise SystemExit(main())
