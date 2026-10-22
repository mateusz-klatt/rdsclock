"""Regression-gate coverage for tools/benchmark.py."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

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


@pytest.mark.parametrize("option", ["--check", "--json"])
@pytest.mark.parametrize("path_kind", ["parent", "absolute", "symlink"])
def test_benchmark_rejects_report_escape_before_decoding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, option: str, path_kind: str
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("preserve", encoding="utf-8")
    if path_kind == "parent":
        report_path = "../outside.json"
    elif path_kind == "absolute":
        report_path = str(outside)
    else:
        (work_dir / "link.json").symlink_to(outside)
        report_path = "link.json"
    monkeypatch.chdir(work_dir)
    decode = Mock(side_effect=AssertionError("must validate before decoding"))
    monkeypatch.setattr(benchmark, "benchmark_file", decode)

    with pytest.raises(SystemExit) as error:
        benchmark.main([str(tmp_path), option, report_path])

    assert error.value.code == 2
    decode.assert_not_called()
    assert outside.read_text(encoding="utf-8") == "preserve"


def test_benchmark_reports_from_external_work_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))
    row = reference["results"][0]
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / row["file"]).write_bytes(b"")
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "nested").mkdir()
    (reports / "reference.json").write_text(json.dumps(reference | {"results": [row]}))
    monkeypatch.chdir(reports)
    monkeypatch.setattr(benchmark, "benchmark_file", Mock(return_value=dict(row)))

    assert (
        benchmark.main(
            [
                str(baseline),
                "--check",
                str(reports / "reference.json"),
                "--json",
                "nested/result.json",
            ]
        )
        == 0
    )

    result = json.loads((reports / "nested/result.json").read_text(encoding="utf-8"))
    assert result["baseline_dir"] == str(baseline)
    assert result["results"][0]["file"] == row["file"]


def test_benchmark_rejects_symlinked_output_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work_dir = tmp_path / "work"
    outside = tmp_path / "outside"
    work_dir.mkdir()
    outside.mkdir()
    (work_dir / "reports").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(work_dir)

    with pytest.raises(SystemExit) as error:
        benchmark.main(["--json", "reports/result.json"])

    assert error.value.code == 2
    assert not (outside / "result.json").exists()


def test_reference_loader_validates_non_cli_callers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    reference = tmp_path / "outside.json"
    reference.write_text('{"results": []}', encoding="utf-8")
    monkeypatch.chdir(work_dir)

    with pytest.raises(ValueError, match="working directory"):
        benchmark._load_reference(reference)
