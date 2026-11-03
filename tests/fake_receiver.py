"""A recorded ``recon --jsonl`` session and a stand-in receiver for UI tests."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import rdsclock.recon as recon
from rdsclock.rds_clock import ClockTime
from rdsclock.time_consensus import ConsensusResult, TrustLevel

HOST_UTC = datetime(2026, 11, 3, 12, 0, 40, tzinfo=UTC)
CT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
MARKUP_PS = "[red]C[/]"

STATION_A = recon.StationCandidate(102.4e6, -18.2, 812, True, 0xA101, "STACJA A")
STATION_B = recon.StationCandidate(98.8e6, -21.5, 643, True, 0xA102, "STACJA B")
STATION_C = recon.StationCandidate(107.5e6, -24.0, 840, True, 0xA103, MARKUP_PS)


def line(event: recon.ReconEvent) -> str:
    return recon.event_to_json(event, host_utc=HOST_UTC)


def cycle(utc: datetime | None = CT + timedelta(seconds=30)) -> recon.CycleCompleted:
    consensus = ConsensusResult(
        utc=utc,
        uncertainty_s=3.0 if utc else float("inf"),
        trust_level=TrustLevel.HIGH if utc else TrustLevel.STALE,
        n_sources=2 if utc else 0,
        contributing_freqs_mhz=[98.8, 102.4] if utc else [],
        outlier_freqs_mhz=[107.5] if utc else [],
        median_age_s=41.0 if utc else float("inf"),
        notes="" if utc else "no active sources",
    )
    tracks = (
        recon.TrackSnapshot(102.4e6, 0xA101, 2, 0.6, 0, 41.0, CT),
        recon.TrackSnapshot(98.8e6, 0xA102, 2, 0.6, 0, 63.0, CT),
        recon.TrackSnapshot(107.5e6, 0xA103, 2, 0.1, 2, 80.0, CT),
    )
    return recon.CycleCompleted(1, 3, consensus, (STATION_A, STATION_B, STATION_C), tracks, 412.0)


def session() -> list[str]:
    """One acquisition and one maintenance cycle, as ``recon --jsonl`` writes them."""
    late_ct = ClockTime(CT.replace(minute=10), 60)
    return [
        line(recon.ReconStarted(tuner_type=5, gain_count=29)),
        line(recon.AcquisitionStarted(1, 98.0, 108.0, 0.1)),
        line(recon.ScanStep(98.8e6, -21.5, STATION_B)),
        line(recon.ScanStep(98.9e6, -60.0, None)),
        line(recon.WatchlistUpdated(1, (STATION_A, STATION_B, STATION_C))),
        line(recon.MaintenanceStarted(1, 3)),
        line(recon.HopResult(102.4e6, 0xA101, "STACJA A", -18.2, 812, 795, 17, ClockTime(CT, 60))),
        line(recon.HopResult(98.8e6, 0xA102, "STACJA B", -21.5, 643, 640, 3, None)),
        line(recon.HopResult(107.5e6, 0xA103, MARKUP_PS, -24.0, 840, 838, 2, late_ct)),
        line(cycle()),
    ]


# Replays a session file, answers SIGUSR1 with a new acquisition and SIGINT
# with recon_stopped, like the real loop. The mode selects a failure to imitate.
FAKE_RECEIVER = """
import json, signal, sys, time

path, mode = sys.argv[1], sys.argv[2]

def emit(record):
    print(json.dumps({"schema": 1, "host_utc": "2026-11-03T12:01:00+00:00", **record}), flush=True)

def rescan(signum, frame):
    emit({"event": "acquisition_started", "iteration": 2,
          "band_start_mhz": 98.0, "band_end_mhz": 108.0, "scan_step_mhz": 0.1})

signal.signal(signal.SIGUSR1, rescan)
if mode == "ignore-sigint":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
if mode == "overlong":
    print("x" * (2 << 20), flush=True)
for line in open(path, encoding="utf-8"):
    print(line, end="", flush=True)
if mode == "fail":
    print("rdsclock: [Errno 111] Connection refused", file=sys.stderr, flush=True)
    sys.exit(1)
try:
    while True:
        time.sleep(0.02)
except KeyboardInterrupt:
    emit({"event": "recon_stopped", "iteration": 1, "reason": "interrupted"})
"""


def receiver_command(tmp_path: Path, mode: str = "follow") -> list[str]:
    """A command that replays :func:`session` like ``rdsclock recon --jsonl``."""
    recording = tmp_path / "session.jsonl"
    recording.write_text("\n".join(session()) + "\n", encoding="utf-8")
    return [sys.executable, "-I", "-c", FAKE_RECEIVER, str(recording), mode]
