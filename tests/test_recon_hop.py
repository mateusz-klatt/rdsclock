"""HOP over a list of stations: recon --hop checks only the listed stations."""

import json

import pytest

import rdsclock.cli as cli
import rdsclock.recon as recon
from tests.fake_receiver import line
from tests.test_dashboard import _state
from tests.test_recon_events import _cfg, _clock, _decode_result, _Receiver

FREQS = (98.8e6, 102.4e6, 107.5e6)


class TestHopList:
    def test_scan_checks_only_the_listed_stations_whatever_their_rssi(self, monkeypatch):
        monkeypatch.setattr(recon, "decode_iq", lambda iq, fs, **kwargs: _decode_result(None))
        events: list[recon.ReconEvent] = []
        receiver = _Receiver([1.0, 0.001, 1.0])  # the second is far below the threshold
        candidates = recon.quick_scan_band(
            receiver, _cfg(hop_freqs_hz=FREQS), on_event=events.append
        )
        assert [event.freq_hz for event in events] == list(FREQS)
        assert [c.freq_hz for c in candidates] == list(FREQS)
        assert events[1].candidate is not None
        assert events[1].rssi_db == pytest.approx(-60.0)

    def test_run_recon_hops_between_every_listed_station_with_rds(
        self, monkeypatch, rtl_tcp_factory
    ):
        results = iter([_decode_result(None), _decode_result(None, groups=0), _decode_result(None)])
        hops = iter([_decode_result(_clock()), _decode_result(_clock())])

        def decode(iq, fs, **kwargs):
            return next(results, None) or next(hops)

        monkeypatch.setattr(recon, "decode_iq", decode)
        events: list[recon.ReconEvent] = []
        status: list[str] = []
        with rtl_tcp_factory(bytes([180, 128] * 256)) as port:
            cfg = _cfg(hop_freqs_hz=FREQS, iterations=1, host="127.0.0.1", port=port)
            recon.run_recon(cfg, on_status=status.append, on_event=events.append)

        kinds = [event.kind for event in events]
        assert kinds[:6] == [
            "recon_started",
            "acquisition_started",
            "scan_step",
            "scan_step",
            "scan_step",
            "watchlist_updated",
        ]
        acquisition, watchlist = events[1], events[5]
        assert (acquisition.band_start_mhz, acquisition.band_end_mhz) == (98.8, 107.5)
        assert (acquisition.scan_step_mhz, acquisition.n_points) == (0.0, 3)
        # Every listed station with RDS stays, although none showed CT yet.
        assert sorted(s.freq_hz for s in watchlist.watchlist) == [98.8e6, 107.5e6]
        assert kinds.count("hop_result") == 2
        assert "\n[#1] ACQUISITION (check 3 listed stations)…" in status
        record = json.loads(recon.event_to_json(acquisition))
        assert record["n_points"] == 3

    def test_band_scans_leave_n_points_empty(self):
        record = json.loads(recon.event_to_json(recon.AcquisitionStarted(1, 98.0, 108.0, 0.1)))
        assert record["n_points"] is None


class TestHopOptions:
    @staticmethod
    def _args(*argv: str):
        return cli.build_parser().parse_args(["recon", *argv])

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            (["--hop", "98.8", "--wide", "102.4"], "choose either --hop or --wide"),
            (["--hop", "98.8,102.4", "--from-dir", "iq"], "--hop applies to live reception"),
            (["--hop", "98.8,x"], "--hop: could not convert"),
            (["--hop", "98.8,98.80"], "--hop: list each station once"),
        ],
    )
    def test_unusable_hop_options_are_refused(self, argv, message, capsys):
        assert cli.cmd_recon(self._args(*argv)) == 2
        assert message in capsys.readouterr().err

    def test_hop_list_reaches_the_config_and_the_dashboard_child(self):
        args = self._args("--hop", "98.8,102.4,107.5")
        assert cli._recon_list_error(args) is None
        cfg = cli._recon_config(args)
        assert (cfg.hop_freqs_hz, cfg.wide_freqs_hz) == (FREQS, ())
        argv = cli._recon_child_argv(args)
        assert "--hop=98.8,102.4,107.5" in argv
        assert cli._recon_config(cli.build_parser().parse_args(argv)) == cfg


class TestHopDashboard:
    def test_a_listed_scan_counts_stations(self):
        started = line(recon.AcquisitionStarted(1, 98.8, 107.5, 0.0, n_points=3))
        step = line(recon.ScanStep(98.8e6, -25.4, None))
        state, _, messages = _state([started, step])
        assert state.view().status == "ACQUISITION #1 · 98.80 MHz · 1/3 stations"
        assert messages == ["Acquisition #1: checking 3 listed stations, 98.80-107.50 MHz."]
        band = line(recon.AcquisitionStarted(2, 98.0, 108.0, 0.1))
        state.apply_line(band)
        assert state.view().status == "ACQUISITION #2 · 0/101 points"
