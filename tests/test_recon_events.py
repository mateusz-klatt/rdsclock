"""Typed recon events and their JSON Lines rendering (``recon --jsonl``)."""

import json
import os
import signal
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import get_args

import numpy as np
import pytest

import rdsclock.cli as cli
import rdsclock.recon as recon
from rdsclock.ct_policy import CTInterpretation
from rdsclock.decoder import DecodeResult
from rdsclock.rds_clock import ClockTime
from rdsclock.rds_groups import StationInfo
from rdsclock.rtl_tcp import FreshCapture
from rdsclock.time_consensus import StationObservation, TimeConsensus, TrustLevel

HOST_UTC = datetime(2026, 11, 3, 12, 0, 5, tzinfo=UTC)


def _clock() -> ClockTime:
    return ClockTime(utc=datetime(2026, 11, 3, 12, 0, tzinfo=UTC), local_offset_minutes=60)


def _decode_result(clock: ClockTime | None, groups: int = 4) -> DecodeResult:
    info = StationInfo(pi=0xCAFE)
    if clock is not None:
        info.clock_times.append(clock)
    return DecodeResult(
        info=info,
        n_groups=groups,
        n_bits=104 * groups,
        freq_offset_hz=0.0,
        symbol_offset=0,
        n_groups_clean=groups - 1,
        n_groups_corrected=1,
    )


class _Receiver:
    """Just enough of ``RtlTcpClient`` for the scan and hop helpers."""

    def __init__(self, amplitudes: list[float]):
        self.amplitudes = iter(amplitudes)

    def configure_fm(self, sample_rate, gain_db=None, ppm=0):
        pass

    def set_frequency(self, value):
        pass

    def capture_fresh(self, duration, fs, **kwargs):
        iq = np.full(max(round(duration * fs), 1), next(self.amplitudes), dtype=np.complex64)
        return FreshCapture(iq, time.monotonic_ns(), 0)


class _Backlogged(_Receiver):
    """A ``_Receiver`` whose captures began 2.5 s behind real time."""

    def __init__(self, amplitudes: list[float], caught_up: bool = True):
        super().__init__(amplitudes)
        self.caught_up = caught_up
        self.captures: list[FreshCapture] = []

    def capture_fresh(self, duration, fs, **kwargs):
        iq = super().capture_fresh(duration, fs, **kwargs).iq
        start_ns = time.monotonic_ns() - 2_500_000_000
        self.captures.append(
            FreshCapture(iq, start_ns, 0, queued_s=2.5, backlogged=True, caught_up=self.caught_up)
        )
        return self.captures[-1]


BACKLOG_MESSAGE = (
    ": the capture began 2.5 s behind real time (queued samples); receive times account for it"
)
BEHIND_MESSAGE = (
    ": the capture began at least 2.5 s behind real time and never caught up; "
    "its Clock-Time stays out of the consensus"
)


def _cfg(**overrides) -> recon.ReconConfig:
    settings = {
        "band_start_mhz": 95.5,
        "band_end_mhz": 95.6,
        "scan_step_mhz": 0.1,
        "scan_dwell_s": 0.001,
        "dwell_s": 0.001,
        "sample_rate": 1000,
        "fs_scan": 1000,
        "rssi_threshold_db": -20.0,
        "idle_s": 0.0,
    }
    settings.update(overrides)
    return recon.ReconConfig(**settings)


def test_event_kinds_are_unique():
    kinds = [event.kind for event in get_args(recon.ReconEvent)]
    assert len(set(kinds)) == len(kinds) == 8


def test_tracks_report_the_tuning_of_their_latest_observation():
    consensus = TimeConsensus()
    now = time.monotonic()
    for freq_hz in (102.40e6, 102.42e6):  # one 100 kHz bin, so one track
        observation = StationObservation(freq_hz, 0xCAFE, _clock().utc, received_monotonic=now)
        consensus.record(observation)
    assert len(consensus.tracks) == 1
    assert consensus.consensus(now).contributing_freqs_mhz == [pytest.approx(102.42)]
    assert recon._track_snapshots(consensus, now)[0].freq_hz == 102.42e6
    assert "102.42MHz PI=0xCAFE" in consensus.summary(now)


class TestEventToJson:
    def test_header_precedes_fields_and_datetimes_are_iso(self):
        event = recon.HopResult(
            freq_hz=95.5e6,
            pi=0xCAFE,
            ps_name="TEST",
            rssi_db=np.float64(-12.5),
            n_groups=4,
            n_groups_clean=3,
            n_groups_corrected=1,
            clock_time=_clock(),
        )
        line = recon.event_to_json(event, host_utc=HOST_UTC)
        assert "\n" not in line
        record = json.loads(line)
        assert list(record)[:3] == ["event", "schema", "host_utc"]
        assert record["event"] == "hop_result"
        assert record["schema"] == recon.JSONL_SCHEMA_VERSION
        assert record["host_utc"] == "2026-11-03T12:00:05+00:00"
        assert record["rssi_db"] == -12.5
        assert record["clock_time"]["utc"] == "2026-11-03T12:00:00+00:00"
        assert record["clock_time"]["local_offset_minutes"] == 60
        assert record["queued_s"] == 0.0
        assert record["caught_up"] is True

    def test_unavailable_consensus_encodes_infinity_as_null(self):
        event = recon.CycleCompleted(
            iteration=1,
            new_observations=0,
            consensus=TimeConsensus().consensus(),
            watchlist=(),
            tracks=(),
            next_rescan_in_s=600.0,
        )
        record = json.loads(recon.event_to_json(event, host_utc=HOST_UTC))
        assert record["consensus"]["utc"] is None
        assert record["consensus"]["uncertainty_s"] is None
        assert record["consensus"]["median_age_s"] is None
        assert record["consensus"]["trust_level"] == "STALE"
        assert record["watchlist"] == []

    def test_scan_step_nests_the_candidate(self):
        candidate = recon.StationCandidate(95.5e6, -12.0, 4, True, np.int64(0xCAFE), "TEST")
        decoded = json.loads(recon.event_to_json(recon.ScanStep(95.5e6, -12.0, candidate)))
        assert decoded["candidate"] == {
            "freq_hz": 95.5e6,
            "rssi_db": -12.0,
            "n_groups": 4,
            "has_ct": True,
            "pi": 0xCAFE,
            "ps_name": "TEST",
        }
        skipped = json.loads(recon.event_to_json(recon.ScanStep(95.6e6, -40.0, None)))
        assert skipped["candidate"] is None

    def test_host_time_defaults_to_now_in_utc(self):
        before = datetime.now(UTC)
        record = json.loads(recon.event_to_json(recon.ReconStopped(1, "iterations")))
        host_utc = datetime.fromisoformat(record["host_utc"])
        assert host_utc.utcoffset() == timedelta(0)
        assert before <= host_utc <= datetime.now(UTC)

    def test_numpy_scalars_become_plain_json(self):
        candidate = recon.StationCandidate(
            np.float32(95.5e6), np.float64("-inf"), np.int64(3), np.bool_(True), np.int16(7)
        )
        record = json.loads(recon.event_to_json(recon.WatchlistUpdated(1, (candidate,))))
        assert record["watchlist"][0] == {
            "freq_hz": 95.5e6,
            "rssi_db": None,
            "n_groups": 3,
            "has_ct": True,
            "pi": 7,
            "ps_name": "",
        }


class TestScanAndHopEvents:
    def test_scan_reports_every_frequency(self, monkeypatch):
        monkeypatch.setattr(recon, "decode_iq", lambda iq, fs, **kwargs: _decode_result(_clock()))
        events: list[recon.ReconEvent] = []
        candidates = recon.quick_scan_band(_Receiver([1.0, 0.001]), _cfg(), on_event=events.append)
        strong, weak = events
        assert strong.candidate == candidates[0]
        assert strong.candidate is not candidates[0]
        assert strong.rssi_db == pytest.approx(0.0, abs=1e-6)
        assert weak.candidate is None
        assert weak.freq_hz == pytest.approx(95.6e6)
        assert weak.rssi_db == pytest.approx(-60.0)

    def test_hop_reports_stations_with_and_without_ct(self, monkeypatch):
        results = iter([_decode_result(_clock()), _decode_result(None, groups=2)])
        monkeypatch.setattr(recon, "decode_iq", lambda iq, fs, **kwargs: next(results))
        watchlist = [
            recon.StationCandidate(95.5e6, -10.0, 4, True, 0xCAFE),
            recon.StationCandidate(96.1e6, -12.0, 2, False, 0xBEEF),
        ]
        events: list[recon.ReconEvent] = []
        n_obs = recon.hop_collect_ct(
            _Receiver([1.0, 1.0]), watchlist, _cfg(), TimeConsensus(), on_event=events.append
        )
        assert n_obs == 1
        assert [event.freq_hz for event in events] == [95.5e6, 96.1e6]
        assert events[0].clock_time == _clock()
        assert events[1].clock_time is None
        assert (events[1].n_groups, events[1].n_groups_clean, events[1].n_groups_corrected) == (
            2,
            1,
            1,
        )

    def test_a_backlogged_hop_is_dated_from_its_capture_and_reported(self, monkeypatch):
        starts: list[int | None] = []

        def decode(iq, fs, capture_start_monotonic_ns=None, **kwargs):
            starts.append(capture_start_monotonic_ns)
            return _decode_result(_clock())

        monkeypatch.setattr(recon, "decode_iq", decode)
        receiver = _Backlogged([1.0])
        events: list[recon.ReconEvent] = []
        messages: list[str] = []
        recon.hop_collect_ct(
            receiver,
            [recon.StationCandidate(95.5e6, -10.0, 4, True, 0xCAFE)],
            _cfg(),
            TimeConsensus(),
            progress=messages.append,
            on_event=events.append,
        )
        assert starts == [receiver.captures[0].capture_start_monotonic_ns]
        assert events[0].queued_s == 2.5
        assert json.loads(recon.event_to_json(events[0]))["queued_s"] == 2.5
        assert messages == [
            "  hop  95.50 MHz" + BACKLOG_MESSAGE,
            "  hop  95.50 MHz: CT=12:00:00Z groups=4",
        ]

    def test_ct_from_a_capture_that_never_caught_up_stays_out_of_the_consensus(self, monkeypatch):
        monkeypatch.setattr(recon, "decode_iq", lambda iq, fs, **kwargs: _decode_result(_clock()))
        consensus = TimeConsensus()
        events: list[recon.ReconEvent] = []
        messages: list[str] = []
        n_obs = recon.hop_collect_ct(
            _Backlogged([1.0], caught_up=False),
            [recon.StationCandidate(95.5e6, -10.0, 4, True, 0xCAFE)],
            _cfg(),
            consensus,
            progress=messages.append,
            on_event=events.append,
        )
        assert n_obs == 0
        assert consensus.tracks == {}
        assert events[0].clock_time == _clock()
        assert not events[0].caught_up
        assert json.loads(recon.event_to_json(events[0]))["caught_up"] is False
        assert messages == [
            "  hop  95.50 MHz" + BEHIND_MESSAGE,
            "  hop  95.50 MHz: CT=12:00:00Z groups=4 (receive time unknown, not used)",
        ]

    def test_a_backlogged_scan_step_is_reported(self, monkeypatch):
        monkeypatch.setattr(recon, "decode_iq", lambda iq, fs, **kwargs: _decode_result(_clock()))
        messages: list[str] = []
        recon.quick_scan_band(_Backlogged([1.0, 0.001]), _cfg(), progress=messages.append)
        assert messages[0] == "  scan  95.50 MHz" + BACKLOG_MESSAGE
        assert messages[2] == "  scan  95.60 MHz" + BACKLOG_MESSAGE


class TestRunReconEvents:
    def test_sequence_mirrors_the_operator_messages(self, monkeypatch, rtl_tcp_factory):
        monkeypatch.setattr(recon, "decode_iq", lambda iq, fs, **kwargs: _decode_result(_clock()))
        evaluations: list[float | None] = []
        evaluate = recon.TimeConsensus.consensus

        def counting(self, monotonic_now=None):
            evaluations.append(monotonic_now)
            return evaluate(self, monotonic_now)

        monkeypatch.setattr(recon.TimeConsensus, "consensus", counting)
        events: list[recon.ReconEvent] = []
        status: list[str] = []
        with rtl_tcp_factory(bytes([180, 128] * 256)) as port:
            cfg = _cfg(band_end_mhz=95.5, iterations=2, host="127.0.0.1", port=port)
            recon.run_recon(cfg, on_status=status.append, on_event=events.append)

        assert [event.kind for event in events] == [
            "recon_started",
            "acquisition_started",
            "scan_step",
            "watchlist_updated",
            "maintenance_started",
            "hop_result",
            "cycle_completed",
            "maintenance_started",
            "hop_result",
            "cycle_completed",
            "recon_stopped",
        ]
        # Every evaluation updates trust, so the status text and the event share one.
        assert len(evaluations) == 2
        cycle = events[-2]
        assert (cycle.iteration, cycle.new_observations, cycle.consensus.n_sources) == (2, 1, 1)
        assert [track.n_observations for track in cycle.tracks] == [2]
        assert events[-1] == recon.ReconStopped(iteration=2, reason="iterations")
        assert any("CONSENSUS:" in message for message in status)

        records = [json.loads(recon.event_to_json(event)) for event in events]
        consensus, track = records[-2]["consensus"], records[-2]["tracks"][0]
        assert datetime.fromisoformat(consensus["utc"]).tzinfo is not None
        assert consensus["trust_level"] == cycle.consensus.trust_level.value
        assert track["pi"] == 0xCAFE
        assert datetime.fromisoformat(track["estimated_utc"]).tzinfo is not None

    def test_requested_rescan_starts_an_acquisition(self, monkeypatch, rtl_tcp_factory):
        monkeypatch.setattr(recon, "decode_iq", lambda iq, fs, **kwargs: _decode_result(_clock()))
        requests = iter([False, True])
        events: list[recon.ReconEvent] = []
        with rtl_tcp_factory(bytes([180, 128] * 256)) as port:
            cfg = _cfg(band_end_mhz=95.5, iterations=2, host="127.0.0.1", port=port)
            recon.run_recon(
                cfg,
                on_status=lambda message: None,
                on_event=events.append,
                rescan_requested=lambda: next(requests),
            )
        scans = [event.iteration for event in events if event.kind == "acquisition_started"]
        assert scans == [1, 2]

    def test_interruption_is_reported(self, monkeypatch, rtl_tcp_factory):
        monkeypatch.setattr(recon, "decode_iq", lambda iq, fs, **kwargs: _decode_result(_clock()))

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(recon, "hop_collect_ct", interrupt)
        events: list[recon.ReconEvent] = []
        with rtl_tcp_factory(bytes([180, 128] * 256)) as port:
            cfg = _cfg(band_end_mhz=95.5, iterations=1, host="127.0.0.1", port=port)
            recon.run_recon(cfg, on_status=lambda message: None, on_event=events.append)
        assert events[-2].kind == "maintenance_started"
        assert events[-1] == recon.ReconStopped(iteration=1, reason="interrupted")

    def test_interruption_while_connecting_still_ends_the_stream(self, monkeypatch):
        class Connecting:
            def __init__(self, host, port):
                pass

            def __enter__(self):
                raise KeyboardInterrupt

            def __exit__(self, *exc_info):
                return False

        monkeypatch.setattr(recon, "RtlTcpClient", Connecting)
        events: list[recon.ReconEvent] = []
        status: list[str] = []
        recon.run_recon(_cfg(), on_status=status.append, on_event=events.append)
        assert events == [recon.ReconStopped(iteration=0, reason="interrupted")]
        assert status == ["\nInterrupted by operator (Ctrl+C)."]

    def test_consumers_cannot_change_the_watchlist(self, monkeypatch, rtl_tcp_factory):
        monkeypatch.setattr(recon, "decode_iq", lambda iq, fs, **kwargs: _decode_result(_clock()))
        log: list[str] = []
        hops: list[float] = []

        def tamper(event: recon.ReconEvent) -> None:
            log.append(event.kind)
            stations = getattr(event, "watchlist", ())
            candidate = getattr(event, "candidate", None)
            for station in (*stations, *([candidate] if candidate else [])):
                station.freq_hz = 0.0
            if event.kind == "hop_result":
                hops.append(event.freq_hz)

        def status(message: str) -> None:
            log.append("status block" if "CONSENSUS:" in message else "text")

        with rtl_tcp_factory(bytes([180, 128] * 256)) as port:
            cfg = _cfg(band_end_mhz=95.5, iterations=2, host="127.0.0.1", port=port)
            recon.run_recon(cfg, on_status=status, on_event=tamper)
        assert hops == [95.5e6, 95.5e6]
        # The cycle record is published before the status text, which may block.
        assert log.index("cycle_completed") < log.index("status block")


class TestRescanSignal:
    def test_each_sigusr1_requests_one_rescan(self):
        before = signal.getsignal(signal.SIGUSR1)
        with cli._rescan_on_sigusr1() as rescan_requested:
            assert not rescan_requested()
            os.kill(os.getpid(), signal.SIGUSR1)
            assert rescan_requested()
            assert not rescan_requested()
        assert signal.getsignal(signal.SIGUSR1) == before

    @pytest.mark.parametrize("extra", [[], ["--jsonl"]])
    def test_live_recon_listens_for_sigusr1(self, monkeypatch, extra):
        seen: list[bool] = []

        def run_recon(cfg, rescan_requested, **callbacks):
            os.kill(os.getpid(), signal.SIGUSR1)
            seen.append(rescan_requested())

        monkeypatch.setattr(cli, "run_recon", run_recon)
        assert cli.main(["recon", *extra]) == 0
        assert seen == [True]


class TestReconJsonlCli:
    def test_interrupt_during_a_write_waits_for_the_whole_record(self, monkeypatch):
        written: list[str] = []

        class InterruptedStdout:
            def write(self, text: str) -> None:
                os.kill(os.getpid(), signal.SIGINT)
                written.append(text)

            def flush(self) -> None:
                pass

        monkeypatch.setattr(sys, "stdout", InterruptedStdout())
        with pytest.raises(KeyboardInterrupt):
            cli._write_event_line(recon.ReconStopped(iteration=1, reason="iterations"))
        (line,) = written
        assert line.endswith("\n")
        assert json.loads(line)["event"] == "recon_stopped"

    def test_offline_replay_is_rejected(self, tmp_path, capsys):
        rc = cli.main(["recon", "--jsonl", "--from-dir", str(tmp_path)])
        captured = capsys.readouterr()
        assert rc == 2
        assert captured.out == ""
        assert "--from-dir" in captured.err

    def test_closed_reader_stops_quietly(self, monkeypatch, tmp_path, capsys):
        def run_recon(cfg, on_status, on_progress, on_event, rescan_requested):
            on_event(recon.ReconStarted(tuner_type=5, gain_count=29))
            raise AssertionError("no events after the reader has gone")

        class ClosedPipe:
            def __init__(self, fd: int):
                self.fd = fd

            def write(self, text):
                raise BrokenPipeError

            def flush(self):
                raise BrokenPipeError

            def fileno(self):
                return self.fd

        monkeypatch.setattr(cli, "run_recon", run_recon)
        path = tmp_path / "stdout"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT)
        try:
            monkeypatch.setattr(sys, "stdout", ClosedPipe(fd))
            rc = cli.main(["recon", "--jsonl"])
            os.write(fd, b"discarded")  # the descriptor now points at /dev/null
        finally:
            os.close(fd)
        assert rc == 1
        assert path.read_bytes() == b""
        assert "rtl_tcp" not in capsys.readouterr().err


# ----------------------- --ct-timezone -----------------------

# 2026-10-31 09:06 real UTC; the Polish stations send CET (10:06) with LTO 0.
REAL_UTC = datetime(2026, 10, 31, 9, 6, tzinfo=UTC)
POLISH = {102.4e6: 0x3211, 98.8e6: 0x3233, 107.5e6: 0x3205, 98.3e6: 0x3245}
HONEST = (95.5e6, 0xCAFE)
WARSAW_RULES = tuple(f"pi:0x{pi:04X}=Europe/Warsaw" for pi in POLISH.values())


def _station_result(pi: int, clock: ClockTime | None) -> DecodeResult:
    info = StationInfo(pi=pi)
    if clock is not None:
        info.clock_times.append(clock)
    return replace(_decode_result(None), info=info)


def _hop_polish_and_honest(monkeypatch, cfg: recon.ReconConfig):
    """Hop the four Polish stations and one honest one; return events, n_obs and consensus."""
    stations = [*POLISH.items(), HONEST]
    results = iter(
        _station_result(pi, ClockTime(REAL_UTC + timedelta(hours=pi != HONEST[1]), 0))
        for _, pi in stations
    )
    monkeypatch.setattr(recon, "decode_iq", lambda iq, fs, **kwargs: next(results))
    watchlist = [recon.StationCandidate(freq, -10.0, 4, True, pi) for freq, pi in stations]
    consensus = TimeConsensus()
    events: list[recon.ReconEvent] = []
    n_obs = recon.hop_collect_ct(
        _Receiver([1.0] * len(stations)), watchlist, cfg, consensus, on_event=events.append
    )
    return events, n_obs, consensus


class TestCtTimezone:
    def test_default_config_reads_every_station_as_standard(self):
        assert recon.ReconConfig().ct_policy == recon.CTPolicy()

    def test_without_rules_the_honest_station_is_the_outlier(self, monkeypatch):
        _, n_obs, consensus = _hop_polish_and_honest(monkeypatch, _cfg())
        result = consensus.consensus(time.monotonic())
        assert n_obs == 5
        assert result.outlier_freqs_mhz == [pytest.approx(95.5)]
        assert abs((result.utc - (REAL_UTC + timedelta(hours=1))).total_seconds()) < 60

    def test_converted_stations_agree_with_an_honest_one(self, monkeypatch):
        cfg = _cfg(ct_policy=recon.CTPolicy.from_specs(WARSAW_RULES))
        events, n_obs, consensus = _hop_polish_and_honest(monkeypatch, cfg)
        result = consensus.consensus(time.monotonic())
        assert n_obs == 5
        assert result.n_sources == 5
        assert result.outlier_freqs_mhz == []
        assert result.trust_level == TrustLevel.HIGH
        assert abs((result.utc - REAL_UTC).total_seconds()) < 60
        # The event keeps the CT as decoded and describes how it was read.
        assert events[0].clock_time == ClockTime(REAL_UTC + timedelta(hours=1), 0)
        assert events[0].ct_interpretation == CTInterpretation(
            REAL_UTC, "Europe/Warsaw", "converted"
        )
        assert events[-1].ct_interpretation == CTInterpretation(REAL_UTC, "UTC", "standard")

    def test_a_ct_without_one_utc_instant_is_reported_but_not_recorded(self, monkeypatch):
        ambiguous = ClockTime(datetime(2026, 10, 25, 2, 30, tzinfo=UTC), 0)
        monkeypatch.setattr(
            recon, "decode_iq", lambda iq, fs, **kwargs: _station_result(0x3211, ambiguous)
        )
        cfg = _cfg(ct_policy=recon.CTPolicy.from_specs(["Europe/Warsaw"]))
        consensus = TimeConsensus()
        events: list[recon.ReconEvent] = []
        messages: list[str] = []
        n_obs = recon.hop_collect_ct(
            _Receiver([1.0]),
            [recon.StationCandidate(102.4e6, -10.0, 4, True, 0x3211)],
            cfg,
            consensus,
            progress=messages.append,
            on_event=events.append,
        )
        assert n_obs == 0
        assert consensus.tracks == {}
        (hop,) = events
        assert hop.clock_time == ambiguous
        assert hop.ct_interpretation == CTInterpretation(None, "Europe/Warsaw", "ambiguous")
        assert messages == [
            "  hop 102.40 MHz: CT=02:30:00 Europe/Warsaw (ambiguous) groups=4 "
            "(no single UTC instant, not used)"
        ]

    def test_a_converted_ct_is_shown_with_its_effective_utc(self, monkeypatch):
        cfg = _cfg(ct_policy=recon.CTPolicy.from_specs(WARSAW_RULES))
        stations = list(POLISH.items())[:1]
        monkeypatch.setattr(
            recon,
            "decode_iq",
            lambda iq, fs, **kwargs: _station_result(
                0x3211, ClockTime(REAL_UTC + timedelta(hours=1), 0)
            ),
        )
        messages: list[str] = []
        recon.hop_collect_ct(
            _Receiver([1.0]),
            [recon.StationCandidate(freq, -10.0, 4, True, pi) for freq, pi in stations],
            cfg,
            TimeConsensus(),
            progress=messages.append,
        )
        assert messages == ["  hop 102.40 MHz: CT=10:06:00 Europe/Warsaw → 09:06:00Z groups=4"]


class TestCtTimezoneJsonl:
    @staticmethod
    def _record(**fields) -> dict:
        event = recon.HopResult(102.4e6, 0x3211, "JEDYNKA", -18.0, 4, 3, 1, **fields)
        return json.loads(recon.event_to_json(event, host_utc=HOST_UTC))

    def test_converted_fields_are_added_inside_clock_time(self):
        ct = ClockTime(REAL_UTC + timedelta(hours=1), 0)
        record = self._record(
            clock_time=ct,
            ct_interpretation=CTInterpretation(REAL_UTC, "Europe/Warsaw", "converted"),
        )
        assert record["schema"] == 1
        assert record["clock_time"] == {
            "utc": "2026-10-31T10:06:00+00:00",
            "local_offset_minutes": 0,
            "rx_monotonic_ns": None,
            "tx_latency_ns": None,
            "effective_utc": "2026-10-31T09:06:00+00:00",
            "ct_timezone": "Europe/Warsaw",
            "ct_status": "converted",
        }
        assert "ct_interpretation" not in record

    def test_a_standard_ct_keeps_its_fields_and_gains_the_reading(self):
        record = self._record(clock_time=_clock())
        assert list(record["clock_time"])[:4] == [
            "utc",
            "local_offset_minutes",
            "rx_monotonic_ns",
            "tx_latency_ns",
        ]
        assert record["clock_time"]["utc"] == "2026-11-03T12:00:00+00:00"
        assert record["clock_time"]["effective_utc"] == "2026-11-03T12:00:00+00:00"
        assert record["clock_time"]["ct_timezone"] == "UTC"
        assert record["clock_time"]["ct_status"] == "standard"

    def test_an_unmappable_ct_has_a_null_effective_utc(self):
        record = self._record(
            clock_time=ClockTime(datetime(2026, 3, 29, 2, 30, tzinfo=UTC), 0),
            ct_interpretation=CTInterpretation(None, "Europe/Warsaw", "nonexistent"),
        )
        assert record["clock_time"]["utc"] == "2026-03-29T02:30:00+00:00"
        assert record["clock_time"]["effective_utc"] is None
        assert record["clock_time"]["ct_status"] == "nonexistent"

    def test_no_ct_stays_null(self):
        record = self._record(clock_time=None)
        assert record["clock_time"] is None
        assert "ct_interpretation" not in record

    def test_hop_events_carry_the_reading_into_jsonl(self, monkeypatch):
        cfg = _cfg(ct_policy=recon.CTPolicy.from_specs(WARSAW_RULES))
        events, _, _ = _hop_polish_and_honest(monkeypatch, cfg)
        clock_times = [json.loads(recon.event_to_json(e))["clock_time"] for e in events]
        assert [c["utc"] for c in clock_times] == ["2026-10-31T10:06:00+00:00"] * 4 + [
            "2026-10-31T09:06:00+00:00"
        ]
        assert {c["effective_utc"] for c in clock_times} == {"2026-10-31T09:06:00+00:00"}
        assert [c["ct_status"] for c in clock_times] == ["converted"] * 4 + ["standard"]


class TestCtTimezoneCli:
    ARGV = ("--ct-timezone=Europe/Warsaw", "--ct-timezone", "pi:0xcafe=UTC")

    def test_rules_reach_the_config(self):
        cfg = cli._recon_config(cli.build_parser().parse_args(["recon", *self.ARGV]))
        assert cfg.ct_policy == recon.CTPolicy.from_specs(["Europe/Warsaw", "pi:0xCAFE=UTC"])
        assert cfg.ct_policy.zone_for(0xCAFE, 95.5e6) == "UTC"
        assert cli._recon_config(cli.build_parser().parse_args(["recon"])).ct_policy == (
            recon.CTPolicy()
        )

    def test_rules_are_forwarded_to_the_dashboard_child(self, monkeypatch):
        import rdsclock.web as web

        commands: list[list[str]] = []
        monkeypatch.setattr(
            web, "run_web", lambda command, host, port, language="en": commands.append(command)
        )
        argv = ["recon", "--hop=102.4,95.5", *self.ARGV, "--ct-timezone=freq:98.8=Europe/Berlin"]
        cli.main([*argv, "--web"])
        (command,) = commands
        assert command[3:5] == ["recon", "--jsonl"]
        assert [arg for arg in command if arg.startswith("--ct-timezone")] == [
            "--ct-timezone=Europe/Warsaw",
            "--ct-timezone=pi:0xCAFE=UTC",
            "--ct-timezone=freq:98.8=Europe/Berlin",
        ]
        parser = cli.build_parser()
        child = cli._recon_config(parser.parse_args(command[3:]))
        assert child == cli._recon_config(parser.parse_args(argv))

    def test_no_rules_forward_nothing(self):
        argv = cli._recon_child_argv(cli.build_parser().parse_args(["recon"]))
        assert not any(arg.startswith("--ct-timezone") for arg in argv)

    @pytest.mark.parametrize(
        "spec,message",
        [
            ("Europe/Warsw", "unknown time zone 'Europe/Warsw'"),
            ("pi:xyz=Europe/Warsaw", "PI must be a hex code"),
            ("freq:x=Europe/Warsaw", "frequency must be a number"),
            ("ssid:1=Europe/Warsaw", "selector must be"),
        ],
    )
    def test_a_bad_spec_is_an_argparse_error(self, spec, message, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["recon", f"--ct-timezone={spec}"])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "argument --ct-timezone" in err
        assert message in err

    def test_conflicting_rules_exit_before_receiving(self, monkeypatch, capsys):
        monkeypatch.setattr(recon, "run_recon", lambda *a, **k: pytest.fail("must not run"))
        rc = cli.main(
            ["recon", "--ct-timezone=pi:0x3211=Europe/Warsaw", "--ct-timezone=pi:3211=UTC"]
        )
        assert rc == 2
        assert "conflicting --ct-timezone rules" in capsys.readouterr().err

    def test_offline_replay_uses_the_rules(self, monkeypatch, tmp_path):
        seen: list[recon.ReconConfig] = []
        monkeypatch.setattr(
            cli, "run_recon_offline", lambda cfg, directory, **kwargs: seen.append(cfg)
        )
        cli.main(["recon", "--from-dir", str(tmp_path), "--ct-timezone=Europe/Warsaw"])
        assert seen[0].ct_policy == recon.CTPolicy.from_specs(["Europe/Warsaw"])
