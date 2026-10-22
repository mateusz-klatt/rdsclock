"""Safety boundaries for the local receiver measurement tool."""

import argparse
import csv
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools import measure_settle


@pytest.mark.parametrize("port", ["1", "1234", "65535"])
def test_tcp_port_accepts_valid_range(port: str) -> None:
    assert measure_settle._tcp_port(port) == int(port)


@pytest.mark.parametrize("port", ["0", "-1", "65536", "1.5", "invalid"])
def test_tcp_port_rejects_invalid_values(port: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="1 to 65535"):
        measure_settle._tcp_port(port)


@pytest.mark.parametrize(
    "arguments",
    [["--host", "example.com"], ["--host", "169.254.169.254"], ["--port", "65536"]],
)
def test_measurement_rejects_unapproved_endpoint_before_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: list[str]
) -> None:
    connection = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(measure_settle.socket, "create_connection", connection)
    monkeypatch.setattr(measure_settle, "ROOT", tmp_path)

    with pytest.raises(SystemExit) as error:
        measure_settle.main(["--strong", "91", "--empty", "89.5", *arguments])

    assert error.value.code == 2
    connection.assert_not_called()
    assert not (tmp_path / "eter").exists()


@pytest.mark.parametrize(
    ("host", "endpoint"),
    [("127.0.0.1", "127.0.0.1"), ("localhost", "127.0.0.1"), ("::1", "::1")],
)
def test_measurement_uses_numeric_loopback_without_dns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, host: str, endpoint: str
) -> None:
    receiver = Mock()
    receiver.recv.side_effect = lambda count: b"\x80\x81" * (count // 2)
    connection = Mock(return_value=receiver)
    monkeypatch.setattr(measure_settle.socket, "create_connection", connection)
    monkeypatch.setattr(measure_settle, "ROOT", tmp_path)

    assert (
        measure_settle.main(
            ["--strong", "91", "--empty", "89.5", "--phase-seconds", "0.2", "--host", host]
        )
        == 0
    )

    connection.assert_called_once_with((endpoint, 1234), timeout=10.0)
    receiver.close.assert_called_once_with()
    output = next((tmp_path / "eter").glob("settle-measure-*"))
    assert (output / "capture.iq").stat().st_size == 3 * 50_000 * 8
    with (output / "pilot-power-vs-time.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.reader(stream))
    assert rows[0] == ["time_seconds", "pilot_rms", "pilot_rel_db", "phase"]
    assert len(rows) > 1
