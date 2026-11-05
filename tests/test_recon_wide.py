"""WIDE observation in recon: one wideband capture records every station."""

import json
import time
from datetime import timedelta

import numpy as np
import pytest

import rdsclock.cli as cli
import rdsclock.recon as recon
from rdsclock.channelizer import ChannelDecodeResult
from rdsclock.ct_policy import CTInterpretation, CTPolicy
from rdsclock.rds_clock import ClockTime
from rdsclock.rtl_tcp import FreshCapture
from rdsclock.time_consensus import TimeConsensus
from tests.test_recon_events import _clock, _decode_result

FREQS = (102.0e6, 102.4e6, 103.0e6)


class _Receiver:
    """Records the receiver settings; every capture is a short constant signal."""

    def __init__(self, queued_s: float = 0.0, caught_up: bool = True):
        self.calls = []
        self.queued_s = queued_s
        self.caught_up = caught_up

    def configure_fm(self, sample_rate, gain_db=None, ppm=0):
        self.calls.append(("configure_fm", sample_rate, gain_db))

    def set_frequency(self, value):
        self.calls.append(("set_frequency", value))

    def capture_fresh(self, duration, fs, **kwargs):
        self.calls.append(("capture_fresh", duration, fs))
        start_ns = time.monotonic_ns() - round(self.queued_s * 1e9)
        backlogged = self.queued_s > 0
        return FreshCapture(
            np.ones(16, dtype=np.complex64),
            start_ns,
            0,
            queued_s=self.queued_s,
            backlogged=backlogged,
            caught_up=self.caught_up,
        )


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

    def test_a_list_longer_than_max_stations_keeps_every_station(self, monkeypatch):
        listed = tuple(f * 1e6 for f in (101.6, 101.9, 102.0, 102.4, 102.7, 103.0))
        results = {f: _decode_result(None) for f in listed}
        monkeypatch.setattr(recon, "decode_channels", _channels(results))
        cfg = _cfg(wide_freqs_hz=listed, wide_center_hz=102.3e6)
        assert len(listed) > cfg.max_watchlist
        watchlist = recon._acquire(_Receiver(), cfg, 1, recon._Outputs(lambda m: None))
        assert sorted(s.freq_hz for s in watchlist) == sorted(listed)

    @pytest.mark.parametrize(
        ("caught_up", "n_recorded", "message"),
        [
            (
                True,
                3,
                "began 4.2 s behind real time (queued samples); receive times account for it",
            ),
            (False, 0, "began at least 4.2 s behind real time and never caught up; its Clock-Time"),
        ],
    )
    def test_a_backlog_is_reported_once_and_carried_by_every_station(
        self, monkeypatch, caught_up, n_recorded, message
    ):
        decode = _channels({f: _decode_result(_clock()) for f in FREQS})
        monkeypatch.setattr(recon, "decode_channels", decode)
        watchlist = [recon.StationCandidate(f, -20.0, 4, True, 0xCAFE) for f in FREQS]
        receiver = _Receiver(queued_s=4.2, caught_up=caught_up)
        consensus, events, messages = TimeConsensus(), [], []
        n_obs = recon.wide_collect_ct(
            receiver, watchlist, _cfg(), consensus, messages.append, events.append
        )
        assert [event.queued_s for event in events] == [4.2] * 3
        assert [event.caught_up for event in events] == [caught_up] * 3
        backlog_messages = [m for m in messages if "behind" in m]
        assert len(backlog_messages) == 1
        assert backlog_messages[0].startswith("  wide: the capture " + message)
        assert n_obs == sum(len(track.observations) for track in consensus.tracks.values())
        assert n_obs == n_recorded
        start_ns = decode.calls[0]["capture_start_monotonic_ns"]
        assert time.monotonic_ns() - start_ns >= 4_200_000_000

    def test_an_empty_watchlist_records_nothing(self):
        receiver = _Receiver()
        assert recon.wide_collect_ct(receiver, [], _cfg(), TimeConsensus()) == 0
        assert receiver.calls == []

    def test_the_centre_defaults_to_one_clear_of_the_stations(self):
        assert recon.ReconConfig(wide_freqs_hz=FREQS).wide_center() == pytest.approx(102.85e6)
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
            (["--wide", "102.0", "--center", "nan"], "--center must be a positive frequency"),
            (["--wide", "102.0", "--center", "-102"], "--center must be a positive frequency"),
            (["--wide", "90,103"], "need 13.40 MS/s with guard bands"),
            (["--wide", "102.0,103.0", "--center", "104"], "102.00 MHz is too close to the edge"),
        ],
    )
    def test_unusable_wide_options_are_refused(self, argv, message, capsys):
        assert cli.cmd_recon(self._args(*argv)) == 2
        assert message in capsys.readouterr().err

    def test_wide_options_reach_the_config_and_the_dashboard_child(self):
        args = self._args("--wide", "102.0,102.4,103.0", "--center", "102.55", "--fs", "2048000")
        assert cli._recon_list_error(args) is None
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
        assert cli._recon_list_error(self._args("--wide", "102.0,102.4,103.0")) is None


def test_wide_maintenance_applies_the_ct_timezone_rules(monkeypatch):
    """A WIDE capture reads each station's CT by its rule before the consensus."""
    local = ClockTime(_clock().utc + timedelta(hours=1), 0)  # CET sent as "UTC"
    results = {
        FREQS[0]: _decode_result(local),
        FREQS[1]: _decode_result(None, groups=2),
        FREQS[2]: _decode_result(_clock()),
    }
    monkeypatch.setattr(recon, "decode_channels", _channels(results))
    cfg = _cfg(ct_policy=CTPolicy.from_specs(["freq:102.0=Europe/Warsaw"]))
    watchlist = [recon.StationCandidate(f, -20.0, 4, True, 0xCAFE) for f in FREQS]
    consensus, events = TimeConsensus(), []
    n_obs = recon.wide_collect_ct(_Receiver(), watchlist, cfg, consensus, on_event=events.append)
    assert n_obs == 2
    assert [event.clock_time for event in events] == [local, None, _clock()]
    assert events[0].ct_interpretation == CTInterpretation(
        _clock().utc, "Europe/Warsaw", "converted"
    )
    assert events[1].ct_interpretation is None
    assert events[2].ct_interpretation.status == "standard"
    stored = [obs.ct_utc for track in consensus.tracks.values() for obs in track.observations]
    assert stored == [_clock().utc, _clock().utc]
    record = json.loads(recon.event_to_json(events[0]))
    assert record["clock_time"]["ct_timezone"] == "Europe/Warsaw"
