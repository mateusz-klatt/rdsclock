#!/usr/bin/env python3
"""Regression benchmark — decode every baseline IQ and emit a comparable summary.

Walks a baseline directory (default: the latest under ``eter/baseline-*``),
decodes each ``live-*MHz-*s.iq`` through the current ``rdsclock`` pipeline,
and prints groups / PI / PS / RT / CT counts side-by-side.

Use this to compare any algorithm tweak against a fixed corpus of IQ samples:

    .venv/bin/python tools/benchmark.py                            # latest baseline
    .venv/bin/python tools/benchmark.py eter/baseline-20260518-... # specific
    .venv/bin/python tools/benchmark.py --json results.json        # machine-readable
    .venv/bin/python tools/benchmark.py --check reference.json      # regression gate
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rdsclock import __version__ as rdsclock_version  # noqa: E402, N812
from rdsclock.decoder import decode_file  # noqa: E402

IQ_PATTERN = re.compile(r"live-([0-9.]+)MHz-(\d+)s\.iq$")
MAX_DECODE_TIME_RATIO = 1.5


def find_latest_baseline() -> Path | None:
    pointer = ROOT / "eter" / ".latest-baseline"
    if pointer.exists():
        path = ROOT / pointer.read_text().strip()
        if path.exists():
            return path
    candidates = sorted((ROOT / "eter").glob("baseline-*"))
    return candidates[-1] if candidates else None


def benchmark_file(iq_path: Path, fs: int = 250_000) -> dict:
    t0 = time.time()
    result = decode_file(str(iq_path), fs=fs)
    elapsed = time.time() - t0
    info = result.info
    return {
        "file": iq_path.name,
        "groups": result.n_groups,
        "groups_clean": result.n_groups_clean,
        "groups_corrected": result.n_groups_corrected,
        "bits": result.n_bits,
        "pi": f"0x{info.pi:04X}" if info.pi is not None else None,
        "pty": info.pty,
        "ps": info.ps_name,
        "rt": info.rt_text,
        "ct_count": len(info.clock_times),
        "latest_ct": str(info.latest_clock) if info.latest_clock else None,
        "group_counts": dict(info.group_counts),
        "decode_seconds": round(elapsed, 2),
    }


def _row_key(row: dict) -> str:
    return str(row.get("station") or row.get("file") or "")


def _rows_by_station(rows: list[dict]) -> dict[str, dict]:
    return {key: row for row in rows if (key := _row_key(row))}


def _load_reference(path: Path) -> list[dict]:
    payload = json.loads(path.read_text())
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError(f"reference JSON has no results list: {path}")
    return results


def check_against_reference(current_rows: list[dict], reference_rows: list[dict]) -> list[str]:
    """Return human-readable benchmark regressions against a reference JSON."""
    current_by_station = _rows_by_station(current_rows)
    failures: list[str] = []

    for reference in reference_rows:
        station = _row_key(reference)
        current = current_by_station.get(station)
        if current is None:
            failures.append(f"{station}: missing from current benchmark")
            continue

        reference_groups = int(reference.get("groups") or 0)
        current_groups = int(current.get("groups") or 0)
        if current_groups < reference_groups:
            failures.append(
                f"{station}: groups dropped from {reference_groups} to {current_groups}"
            )

        reference_pi = reference.get("pi")
        current_pi = current.get("pi")
        if reference_pi is not None and current_pi != reference_pi:
            failures.append(f"{station}: PI changed from {reference_pi} to {current_pi}")

        reference_seconds = float(reference.get("decode_seconds") or 0.0)
        current_seconds = float(current.get("decode_seconds") or 0.0)
        if reference_seconds > 0.0 and current_seconds > reference_seconds * MAX_DECODE_TIME_RATIO:
            failures.append(
                f"{station}: decode time rose from {reference_seconds:.2f}s "
                f"to {current_seconds:.2f}s"
            )

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "baseline_dir",
        nargs="?",
        default=None,
        help="Baseline directory (default: latest under eter/baseline-*)",
    )
    parser.add_argument("--fs", type=int, default=250_000, help="IQ sample rate")
    parser.add_argument("--json", metavar="PATH", help="Also write JSON output here")
    parser.add_argument(
        "--check",
        metavar="PATH",
        help="Compare against reference JSON and exit 2 on regression",
    )
    args = parser.parse_args(argv)

    base = Path(args.baseline_dir) if args.baseline_dir else find_latest_baseline()
    if base is None or not base.exists():
        print("no baseline directory found", file=sys.stderr)
        return 2

    iq_files = sorted(base.glob("live-*MHz-*s.iq"))
    if not iq_files:
        print(f"no IQ files in {base}", file=sys.stderr)
        return 2

    print(f"baseline: {base}")
    print(f"rdsclock: {rdsclock_version}")
    print(f"files:    {len(iq_files)}")
    print()
    print(
        f"{'station':<12} {'groups':>7}  {'PI':>7}  {'PS':<10}  {'CT':>3}  "
        f"{'sec':>5}  RT preview"
    )
    print("-" * 100)

    rows = []
    for iq_path in iq_files:
        m = IQ_PATTERN.search(iq_path.name)
        station = f"{m.group(1)} MHz" if m else iq_path.stem
        row = benchmark_file(iq_path, fs=args.fs)
        row["station"] = station
        rows.append(row)
        rt_preview = (row["rt"][:35] + "…") if len(row["rt"]) > 36 else row["rt"]
        print(
            f"{station:<12} {row['groups']:>7}  {row['pi'] or '—':>7}  "
            f"{row['ps']!r:<12.12}  {row['ct_count']:>3}  "
            f"{row['decode_seconds']:>5.1f}  {rt_preview!r}"
        )

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "rdsclock_version": rdsclock_version,
                    "baseline_dir": str(base.relative_to(ROOT)),
                    "fs": args.fs,
                    "results": rows,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        print(f"\njson: {args.json}")

    if args.check:
        try:
            reference_rows = _load_reference(Path(args.check))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"\ncheck: failed to load reference: {exc}", file=sys.stderr)
            return 2
        failures = check_against_reference(rows, reference_rows)
        if failures:
            print("\ncheck: FAILED", file=sys.stderr)
            for failure in failures:
                print(f"  - {failure}", file=sys.stderr)
            return 2
        print(f"\ncheck: OK ({args.check})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
