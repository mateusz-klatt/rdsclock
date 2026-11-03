"""Typed recon events and their JSON Lines rendering (``recon --jsonl``)."""

import json
import os
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import get_args

import numpy as np
import pytest

import rdsclock.cli as cli
import rdsclock.recon as recon
from rdsclock.decoder import DecodeResult
from rdsclock.rds_clock import ClockTime
from rdsclock.rds_groups import StationInfo
from rdsclock.rtl_tcp import FreshCapture
from rdsclock.time_consensus import StationObservation, TimeConsensus

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
