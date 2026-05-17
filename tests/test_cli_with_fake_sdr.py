"""CLI tests for sub-commands that talk to ``rtl_tcp``."""

from rdsclock.cli import main


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

    def test_with_manual_gain_and_carrier(self, fake_rtl_tcp, tmp_path):
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
