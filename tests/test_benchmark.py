"""Regression-gate coverage for tools/benchmark.py."""

import json
from pathlib import Path

from tools import benchmark

REFERENCE = Path("tests/fixtures/benchmark-reference.json")


def test_benchmark_check_against_reference_exits_zero(monkeypatch, tmp_path):
    """Feed each reference row back through --check; must exit 0."""
    rows_by_file = {
        row["file"]: row for row in json.loads(REFERENCE.read_text(encoding="utf-8"))["results"]
    }

    # tmp_path acts as the baseline dir; put matching .iq stubs so the
    # benchmark module's file discovery walks them in the same order.
    for name in rows_by_file:
        (tmp_path / name).write_bytes(b"")

    def replay_reference_row(iq_path: Path, fs: int = 250_000) -> dict:
        del fs
        return dict(rows_by_file[iq_path.name])

    monkeypatch.setattr(benchmark, "benchmark_file", replay_reference_row)

    assert benchmark.main([str(tmp_path), "--check", str(REFERENCE)]) == 0
