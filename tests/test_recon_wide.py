"""WIDE observation in recon: one wideband capture records every station."""

import json
import time

import numpy as np
import pytest

import rdsclock.cli as cli
import rdsclock.recon as recon
from rdsclock.channelizer import ChannelDecodeResult
from rdsclock.rtl_tcp import FreshCapture
from rdsclock.time_consensus import TimeConsensus
from tests.test_recon_events import _clock, _decode_result

FREQS = (102.0e6, 102.4e6, 103.0e6)


class _Receiver:
    """Records the receiver settings; every capture is a short constant signal."""

    def __init__(self):
        self.calls = []

    def configure_fm(self, sample_rate, gain_db=None, ppm=0):
        self.calls.append(("configure_fm", sample_rate, gain_db))

    def set_frequency(self, value):
        self.calls.append(("set_frequency", value))

    def capture_fresh(self, duration, fs, **kwargs):
        self.calls.append(("capture_fresh", duration, fs))
        return FreshCapture(np.ones(16, dtype=np.complex64), time.monotonic_ns(), 0)


def _channels(results: dict):
    """A ``decode_channels`` that returns ``results`` by frequency and records its calls."""

    def decode_channels(**kwargs):
        decode_channels.calls.append(kwargs)
        return [
            ChannelDecodeResult(spec, 4, results[spec.freq_hz], rssi_db=-20.0 - index)
            for index, spec in enumerate(kwargs["channels"])
        ]

    decode_channels.calls = []
    return decode_channels


def _cfg(**overrides) -> recon.ReconConfig:
    settings = {
        "wide_freqs_hz": FREQS,
        "wide_center_hz": 102.85e6,
        "scan_dwell_s": 3.0,
        "dwell_s": 75.0,
        "gain_db": 28.0,
        "idle_s": 0.0,
    }
    settings.update(overrides)
    return recon.ReconConfig(**settings)


class TestWideEngine:
    def test_scan_decodes_every_station_from_one_capture(self, monkeypatch):
        results = {
            FREQS[0]: _decode_result(None, groups=0),
            FREQS[1]: _decode_result(_clock()),
            FREQS[2]: _decode_result(None),
        }
        decode = _channels(results)
        monkeypatch.setattr(recon, "decode_channels", decode)
        receiver, events = _Receiver(), []
        candidates = recon.wide_scan(receiver, _cfg(), on_event=events.append)
        assert receiver.calls == [
            ("configure_fm", 2_400_000, 28.0),
            ("set_frequency", 102_850_000),
            ("capture_fresh", 3.0, 2_400_000),
        ]
        assert [c.freq_hz for c in candidates] == list(FREQS)
        assert [c.has_ct for c in candidates] == [False, True, False]
        assert [c.rssi_db for c in candidates] == [-20.0, -21.0, -22.0]
        assert [event.candidate for event in events] == candidates
        assert events[1].candidate is not candidates[1]
        assert decode.calls[0]["max_workers"] == 1

    def test_maintenance_records_every_station_from_one_capture(self, monkeypatch):
        results = {
            FREQS[0]: _decode_result(_clock()),
            FREQS[1]: _decode_result(None, groups=2),
            FREQS[2]: _decode_result(_clock()),
        }
        decode = _channels(results)
        monkeypatch.setattr(recon, "decode_channels", decode)
        watchlist = [recon.StationCandidate(f, -20.0, 4, True, 0xCAFE) for f in FREQS]
        receiver, events, messages = _Receiver(), [], []
        n_obs = recon.wide_collect_ct(
            receiver,
            watchlist,
            _cfg(),
            TimeConsensus(),
            progress=messages.append,
            on_event=events.append,
        )
        assert n_obs == 2
        assert [call for call in receiver.calls if call[0] == "capture_fresh"] == [
            ("capture_fresh", 75.0, 2_400_000)
        ]
        assert [event.freq_hz for event in events] == list(FREQS)
        assert [event.clock_time for event in events] == [_clock(), None, _clock()]
        assert decode.calls[0]["capture_start_monotonic_ns"] is not None
        assert messages[0].startswith("  wide 102.00 MHz: CT=")
        assert messages[1] == "  wide 102.40 MHz: no CT (groups=2)"

    def test_an_empty_watchlist_records_nothing(self):
        receiver = _Receiver()
        assert recon.wide_collect_ct(receiver, [], _cfg(), TimeConsensus()) == 0
        assert receiver.calls == []

    def test_the_centre_defaults_to_one_clear_of_the_stations(self):
        assert recon.ReconConfig(wide_freqs_hz=FREQS).wide_center() == pytest.approx(102.55e6)
        assert _cfg().wide_center() == 102.85e6

    def test_run_recon_reports_wide_captures(self, monkeypatch, rtl_tcp_factory):
        results = {f: _decode_result(_clock() if f == FREQS[1] else None) for f in FREQS}
        monkeypatch.setattr(recon, "decode_channels", _channels(results))
        events: list[recon.ReconEvent] = []
        status: list[str] = []
        with rtl_tcp_factory(bytes([180, 128] * 256)) as port:
            cfg = _cfg(scan_dwell_s=0.001, dwell_s=0.001, iterations=1, host="127.0.0.1", port=port)
            recon.run_recon(cfg, on_status=status.append, on_event=events.append)

        assert [event.kind for event in events] == [
            "recon_started",
            "acquisition_started",
            "scan_step",
            "scan_step",
            "scan_step",
            "watchlist_updated",
            "maintenance_started",
            "hop_result",
            "hop_result",
            "hop_result",
            "cycle_completed",
            "recon_stopped",
        ]
        acquisition, watchlist, maintenance = events[1], events[5], events[6]
        assert (acquisition.band_start_mhz, acquisition.band_end_mhz) == (102.0, 103.0)
        assert acquisition.scan_step_mhz == 0.0
        assert (acquisition.wide_center_hz, acquisition.wide_sample_rate) == (102.85e6, 2_400_000)
        # Unlike HOP, WIDE keeps every station with RDS, not only those with CT.
        assert [s.freq_hz for s in watchlist.watchlist] == [FREQS[1], FREQS[0], FREQS[2]]
        assert (maintenance.n_stations, maintenance.wide_center_hz) == (3, 102.85e6)
        assert "\n[#1] ACQUISITION (WIDE capture at 102.850 MHz)…" in status
        assert "\n[#1] MAINTENANCE (WIDE capture of 3 stations at 102.850 MHz)…" in status
        record = json.loads(recon.event_to_json(maintenance))
        assert (record["wide_center_hz"], record["wide_sample_rate"]) == (102.85e6, 2_400_000)

    def test_hop_events_leave_the_wide_fields_empty(self):
        record = json.loads(recon.event_to_json(recon.MaintenanceStarted(1, 2)))
        assert (record["wide_center_hz"], record["wide_sample_rate"]) == (None, None)


class TestWideOptions:
    @staticmethod
    def _args(*argv: str):
        return cli.build_parser().parse_args(["recon", *argv])

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            (["--fs", "2400000"], "--center and --fs apply only to --wide"),
            (["--center", "102.85"], "--center and --fs apply only to --wide"),
            (["--wide", "102.0,102.4", "--from-dir", "iq"], "--wide applies to live reception"),
            (["--wide", "102.0,abc"], "--wide: could not convert"),
            (["--wide", "102.0,-1"], "--wide: frequencies must be positive"),
            (["--wide", "102.0,102.00"], "--wide: list each station once"),
            (["--wide", "102.0", "--fs", "100000"], "--fs must be at least 250000 S/s"),
            (["--wide", "90,103"], "need 13.40 MS/s with guard bands"),
            (["--wide", "102.0,103.0", "--center", "104"], "102.00 MHz is too close to the edge"),
        ],
    )
    def test_unusable_wide_options_are_refused(self, argv, message, capsys):
        assert cli.cmd_recon(self._args(*argv)) == 2
        assert message in capsys.readouterr().err

    def test_wide_options_reach_the_config_and_the_dashboard_child(self):
        args = self._args("--wide", "102.0,102.4,103.0", "--center", "102.55", "--fs", "2048000")
        assert cli._recon_wide_error(args) is None
        cfg = cli._recon_config(args)
        assert cfg.wide_freqs_hz == (102.0e6, 102.4e6, 103.0e6)
        assert (cfg.wide_center_hz, cfg.wide_sample_rate) == (102.55e6, 2_048_000)
        argv = cli._recon_child_argv(args)
        assert argv[-3:] == ["--wide=102.0,102.4,103.0", "--center=102.55", "--fs=2048000"]
        assert cli._recon_config(cli.build_parser().parse_args(argv)) == cfg

    def test_defaults_leave_hop_alone_and_pick_the_wide_rate(self):
        assert cli._recon_config(self._args()).wide_freqs_hz == ()
        wide = cli._recon_config(self._args("--wide", "102.0,102.4,103.0"))
        assert (wide.wide_center_hz, wide.wide_sample_rate) == (None, recon.WIDE_SAMPLE_RATE)
        assert cli._recon_wide_error(self._args("--wide", "102.0,102.4,103.0")) is None
