"""Regression-gate coverage for tools/benchmark.py."""

import json
from pathlib import Path

from tools import benchmark

REFERENCE = Path("eter/baseline-20260518-035918/benchmark-0.4.0.json")
BASELINE_DIR = Path("eter/baseline-20260518-035918")


def test_benchmark_check_040_against_itself_exits_zero(monkeypatch):
    rows_by_file = {
        row["file"]: row for row in json.loads(REFERENCE.read_text(encoding="utf-8"))["results"]
    }

    def replay_reference_row(iq_path: Path, fs: int = 250_000) -> dict:
        del fs
        return dict(rows_by_file[iq_path.name])

    monkeypatch.setattr(benchmark, "benchmark_file", replay_reference_row)

    assert benchmark.main([str(BASELINE_DIR), "--check", str(REFERENCE)]) == 0
