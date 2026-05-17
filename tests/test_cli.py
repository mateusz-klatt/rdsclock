"""End-to-end smoke tests for the CLI.

We invoke ``rdsclock.cli.main`` with arguments and check that the
no-SDR sub-commands (`generate`, `decode`, `demo`, `plot`) exit
cleanly. Sub-commands that talk to ``rtl_tcp`` (`live`, `multi`,
`scan`, `recon`) are covered by other tests; here we still exercise
their argparse construction via the help output.

These tests also bring the ``cli.py`` module into line-coverage range,
which is otherwise the largest unreached module in the package.
"""

import pytest

# matplotlib is required by the ``plot`` sub-command; if it is missing
# we skip those specific tests rather than fail.
matplotlib = pytest.importorskip("matplotlib")
matplotlib.use("Agg")

from rdsclock.cli import build_parser, main  # noqa: E402  (must follow Agg backend setup)

SUBCOMMANDS = ["generate", "decode", "live", "multi", "demo", "recon", "scan", "plot", "play"]


class TestBuildParser:
    def test_top_level_parser(self):
        parser = build_parser()
        assert parser.prog == "rdsclock"

    @pytest.mark.parametrize("subcmd", SUBCOMMANDS)
    def test_subcommand_help(self, subcmd, capsys):
        """Running ``rdsclock <sub> --help`` must exit cleanly (SystemExit(0))
        — this also covers the argparse setup branch of each sub-command."""
        with pytest.raises(SystemExit) as exc:
            main([subcmd, "--help"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert subcmd in captured.out or "usage" in captured.out.lower()


class TestCmdGenerate:
    def test_default_options(self, tmp_path, capsys):
        out = tmp_path / "synth.iq"
        rc = main(
            [
                "generate",
                str(out),
                "--time",
                "2026-05-17T12:00",
                "--duration",
                "1.0",
                "--snr",
                "25",
                "--seed",
                "42",
            ]
        )
        assert rc == 0
        assert out.exists()
        assert out.stat().st_size > 0

    def test_no_noise_flag(self, tmp_path):
        out = tmp_path / "clean.iq"
        rc = main(
            [
                "generate",
                str(out),
                "--duration",
                "0.5",
                "--no-noise",
                "--seed",
                "0",
            ]
        )
        assert rc == 0
        assert out.exists()

    def test_u8_format(self, tmp_path):
        out = tmp_path / "u8.iq"
        rc = main(
            [
                "generate",
                str(out),
                "--duration",
                "0.5",
                "--no-noise",
                "--format",
                "u8",
            ]
        )
        assert rc == 0
        assert out.exists()


class TestCmdDecode:
    def test_decode_synthetic(self, tmp_path):
        iq = tmp_path / "x.iq"
        main(
            [
                "generate",
                str(iq),
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
        rc = main(["decode", str(iq)])
        assert rc == 0

    def test_decode_verbose(self, tmp_path, capsys):
        iq = tmp_path / "x.iq"
        main(["generate", str(iq), "--duration", "1.0", "--no-noise", "--seed", "0"])
        rc = main(["decode", str(iq), "-v"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "fm_demod" in out or "channel_filter" in out

    def test_decode_with_carrier_override(self, tmp_path):
        iq = tmp_path / "x.iq"
        main(["generate", str(iq), "--duration", "1.0", "--no-noise", "--seed", "0"])
        rc = main(["decode", str(iq), "--carrier-hz", "57000"])
        assert rc == 0


class TestCmdDemo:
    def test_demo_runs(self, capsys):
        rc = main(["demo", "--time", "2026-05-17T12:00", "--duration", "2.5", "--seed", "42"])
        # Return code 0 means all three stations decoded correctly; 1 means
        # one of them dropped under the random IQ realisation. Either is a
        # successful CLI execution from the test's perspective.
        assert rc in (0, 1)
        out = capsys.readouterr().out
        assert "multi-station synthetic demonstration" in out
        assert "Stations with correct CT" in out


class TestCmdPlot:
    def test_plot_mpx(self, tmp_path):
        iq = tmp_path / "x.iq"
        png = tmp_path / "spec.png"
        main(["generate", str(iq), "--duration", "1.0", "--no-noise", "--seed", "0"])
        rc = main(["plot", str(iq), "--out", str(png)])
        assert rc == 0
        assert png.exists()

    def test_plot_waterfall(self, tmp_path):
        iq = tmp_path / "x.iq"
        png = tmp_path / "wf.png"
        main(["generate", str(iq), "--duration", "0.5", "--no-noise", "--seed", "0"])
        rc = main(["plot", str(iq), "--kind", "waterfall", "--out", str(png)])
        assert rc == 0
        assert png.exists()

    def test_plot_with_title(self, tmp_path):
        iq = tmp_path / "x.iq"
        png = tmp_path / "spec.png"
        main(["generate", str(iq), "--duration", "0.5", "--no-noise", "--seed", "0"])
        rc = main(["plot", str(iq), "--out", str(png), "--title", "Custom title"])
        assert rc == 0


class TestCliEntryPoint:
    def test_main_without_args_errors_cleanly(self):
        """Bare `rdsclock` should fail because no sub-command was given."""
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code != 0

    def test_main_unknown_subcommand(self):
        with pytest.raises(SystemExit) as exc:
            main(["does-not-exist"])
        assert exc.value.code != 0
