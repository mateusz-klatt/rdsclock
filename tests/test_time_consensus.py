"""Tests for multi-source time consensus + per-station trust."""

from datetime import UTC, datetime, timedelta

from rdsclock.time_consensus import (
    StationObservation,
    StationTrack,
    TimeConsensus,
    TrustLevel,
)


def _obs(freq_mhz: float, pi: int, utc: datetime, mono: float) -> StationObservation:
    return StationObservation(
        freq_hz=freq_mhz * 1e6,
        pi=pi,
        ct_utc=utc,
        received_monotonic=mono,
    )


class TestConsensus:
    def test_single_station(self):
        c = TimeConsensus()
        now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        c.record(_obs(92.0, 0x3201, now, mono=100.0))
        result = c.consensus(monotonic_now=100.5)
        assert result.utc is not None
        assert result.n_sources == 1
        # Single source = LOW (or MEDIUM if fresh and close)
        assert result.trust_level in (TrustLevel.LOW, TrustLevel.MEDIUM)
        # Uncertainty is at least 1s (default minimum)
        assert result.uncertainty_s >= 1.0

    def test_three_stations_agree(self):
        c = TimeConsensus()
        utc = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        c.record(_obs(92.0, 0x3201, utc, mono=100.0))
        c.record(_obs(98.3, 0x4321, utc + timedelta(seconds=5), mono=100.0))
        c.record(_obs(106.8, 0x5555, utc + timedelta(seconds=-3), mono=100.0))
        result = c.consensus(monotonic_now=100.5)
        assert result.utc is not None
        assert result.n_sources == 3
        assert result.trust_level == TrustLevel.HIGH
        assert len(result.outlier_freqs_mhz) == 0

    def test_outlier_detected(self):
        c = TimeConsensus()
        utc = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        # 3 stations agree within +/-10s
        c.record(_obs(92.0, 0x3201, utc, mono=100.0))
        c.record(_obs(98.3, 0x4321, utc + timedelta(seconds=5), mono=100.0))
        c.record(_obs(106.8, 0x5555, utc + timedelta(seconds=-2), mono=100.0))
        # 4th station: 5 minutes ahead (outlier!)
        c.record(_obs(107.5, 0x6789, utc + timedelta(minutes=5), mono=100.0))
        result = c.consensus(monotonic_now=100.5)
        assert 107.5 in result.outlier_freqs_mhz
        assert result.n_sources == 3
        # The outlier's trust should drop
        outlier_track = c.tracks[(107_500_000, 0x6789)]
        assert outlier_track.consecutive_outliers >= 1
        assert outlier_track.trust_score < 0.5

    def test_stale_when_old(self):
        c = TimeConsensus()
        utc = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        c.record(_obs(92.0, 0x3201, utc, mono=100.0))
        # Check 10 min later with no new observations
        result = c.consensus(monotonic_now=100.0 + 600.0)
        assert result.trust_level in (TrustLevel.STALE, TrustLevel.LOW)
        # Uncertainty should grow over time (drift)
        assert result.uncertainty_s > 1.0

    def test_drift_grows_uncertainty(self):
        # High ppm + long stale_age to show linear growth
        c = TimeConsensus(local_osc_ppm=10_000.0, stale_age_s=10_000.0)
        utc = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        c.record(_obs(92.0, 0x3201, utc, mono=100.0))
        r1 = c.consensus(monotonic_now=1100.0)  # age = 1000s, drift = 10s
        r2 = c.consensus(monotonic_now=2100.0)  # age = 2000s, drift = 20s
        assert r2.uncertainty_s > r1.uncertainty_s
        assert r2.uncertainty_s >= 10

    def test_repeated_outlier_drops_trust(self):
        c = TimeConsensus()
        utc = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        # 3 good observations from station A
        c.record(_obs(92.0, 0x3201, utc, mono=100.0))
        c.record(_obs(98.3, 0x4321, utc, mono=100.0))
        # Station B - always 2 minutes ahead
        for i in range(3):
            c.record(_obs(92.0, 0x3201, utc + timedelta(seconds=i), mono=100 + i))
            c.record(_obs(98.3, 0x4321, utc + timedelta(seconds=i), mono=100 + i))
            c.record(_obs(107.5, 0x6789, utc + timedelta(minutes=2, seconds=i), mono=100 + i))
            c.consensus(monotonic_now=100 + i + 0.5)
        bad = c.tracks[(107_500_000, 0x6789)]
        assert bad.consecutive_outliers >= 2
        assert bad.trust_score < 0.3

    def test_no_observations(self):
        c = TimeConsensus()
        result = c.consensus(monotonic_now=0.0)
        assert result.utc is None
        assert result.trust_level == TrustLevel.STALE
        assert result.n_sources == 0

    def test_display_format(self):
        c = TimeConsensus()
        utc = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        c.record(_obs(92.0, 0x3201, utc, mono=100.0))
        c.record(_obs(98.3, 0x4321, utc, mono=100.0))
        c.record(_obs(106.8, 0x5555, utc, mono=100.0))
        result = c.consensus(monotonic_now=100.1)
        display = result.format_display()
        assert "UTC 2026-05-17 12:00" in display
        assert "N=3" in display
        assert "trust=HIGH" in display

    def test_estimated_utc_advances_with_monotonic(self):
        t = StationTrack(freq_hz=92e6, pi=0x3201)
        utc = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        t.add_observation(_obs(92.0, 0x3201, utc, mono=100.0))
        est_at_105 = t.estimated_utc_now(monotonic_now=105.0)
        assert est_at_105 == utc + timedelta(seconds=5)


class TestTrackBookkeeping:
    def test_observations_trimmed(self):
        t = StationTrack(freq_hz=92e6, pi=0x3201)
        utc = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        for i in range(100):
            t.add_observation(_obs(92.0, 0x3201, utc + timedelta(seconds=i), mono=100 + i))
        # It should keep only ~30 most recent ones
        assert len(t.observations) <= 30

    def test_track_key_groups_same_station(self):
        c = TimeConsensus()
        utc = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        # same PI, slightly different freq (+/-50 kHz within the 100 kHz bucket)
        c.record(_obs(92.02, 0x3201, utc, mono=100.0))
        c.record(_obs(91.98, 0x3201, utc + timedelta(seconds=1), mono=101.0))
        # They should end up in the same track
        assert len(c.tracks) == 1
