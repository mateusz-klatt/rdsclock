"""CLI tests for sub-commands that talk to ``rtl_tcp``."""

import json
import time
from datetime import UTC, datetime

import numpy as np

import rdsclock.cli as cli
from rdsclock.cli import main
from rdsclock.decoder import DecodeResult
from rdsclock.rds_clock import ClockTime
from rdsclock.rds_groups import StationInfo
from rdsclock.rtl_tcp import FreshCapture, RtlTcpClient


class TestCmdLive:
    def test_default_run(self, fake_rtl_tcp, capsys):
        rc = main(
            [
                "live",
                "--freq",
                "95.5",
                "--duration",
                "1.0",
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Recording" in out
        assert "Groups:" in out

    def test_with_manual_gain_and_carrier(self, fake_rtl_tcp, tmp_path, capsys):
        save_path = tmp_path / "saved.iq"
        rc = main(
            [
                "live",
                "--freq",
                "95.5",
                "--duration",
                "1.0",
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
                "--gain",
                "30",
                "--ppm",
                "5",
                "--carrier-hz",
                "57000",
                "--save",
                str(save_path),
                "-v",
            ]
        )
        assert rc == 0
        assert save_path.exists()
        assert "queued when the capture began:" in capsys.readouterr().out

    def test_a_capture_that_never_caught_up_is_not_compared_with_the_host(
        self, fake_rtl_tcp, capsys, monkeypatch
    ):
        def capture_fresh(self, duration_s, sample_rate, **kwargs):
            iq = np.zeros(round(duration_s * sample_rate), dtype=np.complex64)
            start_ns = time.monotonic_ns() - 3_000_000_000
            return FreshCapture(iq, start_ns, 0, queued_s=3.0, backlogged=True, caught_up=False)

        info = StationInfo(pi=0xCAFE)
        info.clock_times.append(
            ClockTime(datetime(2026, 11, 3, 12, 0, tzinfo=UTC), 60, rx_monotonic_ns=1)
        )
        decoded = DecodeResult(
            info=info, n_groups=1, n_bits=104, freq_offset_hz=0.0, symbol_offset=0
        )
        monkeypatch.setattr(RtlTcpClient, "capture_fresh", capture_fresh)
        monkeypatch.setattr(cli, "decode_iq", lambda iq, **kwargs: decoded)
        argv = ["live", "--freq", "95.5", "--duration", "0.01", "--port", str(fake_rtl_tcp)]
        assert main([*argv, "--host", "127.0.0.1"]) == 0
        out = capsys.readouterr().out
        assert "at least 3.0 s behind real time and never caught up" in out
        assert "CT versus host UTC" not in out


class TestCmdScan:
    def test_short_sweep(self, fake_rtl_tcp, capsys):
        rc = main(
            [
                "scan",
                "--start",
                "95.5",
                "--end",
                "95.7",
                "--step",
                "0.1",
                "--duration",
                "0.5",
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
                "--gain",
                "30",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "Scanning" in out


class TestCmdMulti:
    def test_wide_mode(self, fake_rtl_tcp, capsys):
        rc = main(
            [
                "multi",
                "--freqs",
                "95.5,96.0",
                "--mode",
                "wide",
                "--fs",
                "2400000",
                "--duration",
                "0.5",
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "WIDE mode" in out

    def test_hop_mode(self, fake_rtl_tcp, capsys):
        rc = main(
            [
                "multi",
                "--freqs",
                "92.0,98.3,106.8",
                "--mode",
                "hop",
                "--duration",
                "0.5",
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "HOP mode" in out

    def test_wide_rejected_when_too_narrow(self, fake_rtl_tcp, capsys):
        # 92.0 / 98.3 / 106.8 span 14.8 MHz, far above 2.4 MS/s.
        rc = main(
            [
                "multi",
                "--freqs",
                "92.0,98.3,106.8",
                "--mode",
                "wide",
                "--fs",
                "2400000",
                "--duration",
                "0.5",
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
            ]
        )
        assert rc == 2


class TestCmdRecon:
    def test_one_iteration(self, fake_rtl_tcp, capsys):
        rc = main(
            [
                "recon",
                "--start",
                "95.5",
                "--end",
                "95.7",
                "--step",
                "0.1",
                "--scan-dwell",
                "0.5",
                "--dwell",
                "1.0",
                "--idle",
                "0",
                "--iterations",
                "1",
                "--rssi-threshold",
                "-200",  # accept anything
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
                "--gain",
                "30",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "ACQUISITION" in out
        assert "MAINTENANCE" in out

    def test_jsonl_events_on_stdout_and_text_on_stderr(self, fake_rtl_tcp, capsys):
        rc = main(
            [
                "recon",
                "--jsonl",
                "-v",
                "--start",
                "95.5",
                "--end",
                "95.6",
                "--step",
                "0.1",
                "--scan-dwell",
                "0.5",
                "--dwell",
                "1.0",
                "--idle",
                "0",
                "--iterations",
                "1",
                "--rssi-threshold",
                "-200",
                "--port",
                str(fake_rtl_tcp),
                "--host",
                "127.0.0.1",
                "--gain",
                "30",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        kinds = [json.loads(line)["event"] for line in captured.out.splitlines()]
        assert kinds[:3] == ["recon_started", "acquisition_started", "scan_step"]
        assert kinds.count("scan_step") == 2
        assert kinds[-2:] == ["cycle_completed", "recon_stopped"]
        assert "ACQUISITION" in captured.err
        assert "▸" in captured.err

    def test_offline_mode_via_cli(self, tmp_path, capsys):
        # First synthesise two IQ files into a fresh directory.
        iq_dir = tmp_path / "iq"
        iq_dir.mkdir()
        for freq in ["fm_092.00_MHz.iq", "fm_098.30_MHz.iq"]:
            main(
                [
                    "generate",
                    str(iq_dir / freq),
                    "--time",
                    "2026-05-17T12:00",
                    "--duration",
                    "2.0",
                    "--snr",
                    "30",
                    "--seed",
                    "0",
                ]
            )
        capsys.readouterr()  # discard generate output

        rc = main(["recon", "--from-dir", str(iq_dir)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "OFFLINE RECON" in out
