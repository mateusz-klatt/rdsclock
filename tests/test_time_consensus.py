"""Tests for multi-source time consensus + per-station trust."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from rdsclock.rds_clock import ClockTime
from rdsclock.time_consensus import (
    StationObservation,
    StationTrack,
    SubSecondEstimate,
    TimeConsensus,
    TrustLevel,
)


def _obs(freq_mhz: float, pi: int | None, utc: datetime, mono: float) -> StationObservation:
    return StationObservation(
        freq_hz=freq_mhz * 1e6,
        pi=pi,
        ct_utc=utc,
        received_monotonic=mono,
    )


def _timestamped_obs(
    freq_mhz: float,
    pi: int,
    utc: datetime,
    latency_ns: int,
    mono_origin_ns: int,
    base_utc: datetime,
) -> StationObservation:
    declared_offset_ns = int((utc - base_utc).total_seconds() * 1_000_000_000)
    rx_monotonic_ns = mono_origin_ns + declared_offset_ns + latency_ns
    ct = ClockTime(utc=utc, local_offset_minutes=0, rx_monotonic_ns=rx_monotonic_ns)
    return StationObservation(
        freq_hz=freq_mhz * 1e6,
        pi=pi,
        ct_utc=utc,
        received_monotonic=rx_monotonic_ns / 1_000_000_000,
        clock_time=ct,
        received_wall_utc=utc + timedelta(seconds=latency_ns / 1_000_000_000),
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

    def test_two_equal_camps_an_hour_apart_give_no_time(self):
        c = TimeConsensus()
        utc = datetime(2026, 11, 5, 8, 49, tzinfo=UTC)
        c.record(_obs(92.0, 0x3201, utc, mono=100.0))
        c.record(_obs(98.3, 0x4321, utc, mono=100.0))
        c.record(_obs(98.8, 0x3233, utc + timedelta(hours=1), mono=100.0))
        c.record(_obs(102.4, 0x3211, utc + timedelta(hours=1), mono=100.0))
        result = c.consensus(monotonic_now=100.5)
        # The median, 09:19, is a time no station sent.
        assert result.utc is None
        assert (result.n_sources, result.notes) == (0, "outlier-only")
        assert result.trust_level == TrustLevel.STALE
        assert len(result.outlier_freqs_mhz) == 4

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

    def test_receive_timestamp_excludes_capture_and_decode_delay(self):
        utc = datetime(2026, 10, 31, 9, 6, tzinfo=UTC)
        c = TimeConsensus()
        track = c.record(
            StationObservation(
                freq_hz=102.4e6,
                pi=0x3211,
                ct_utc=utc,
                received_monotonic=110.0,
                clock_time=ClockTime(utc, 0, rx_monotonic_ns=101_000_000_000),
            )
        )

        assert track.age_s(110.0) == 9.0
        assert c.consensus(monotonic_now=110.0).utc == utc + timedelta(seconds=9)

    def test_staleness_uses_receipt_even_when_decoded_recently(self):
        utc = datetime(2026, 10, 31, 9, 6, tzinfo=UTC)
        c = TimeConsensus(stale_age_s=30.0)
        c.record(StationObservation(102.4e6, 0x3211, utc, 150.0, rx_monotonic_ns=100_000_000_000))
        assert c.consensus(monotonic_now=150.0).utc is None

    def test_future_observation_does_not_contribute(self):
        c = TimeConsensus()
        c.record(_obs(102.4, 0x3211, datetime(2026, 10, 31, 9, 6, tzinfo=UTC), mono=110.0))
        assert c.consensus(monotonic_now=100.0).utc is None

    def test_repeated_pi_does_not_inflate_confidence(self):
        c = TimeConsensus()
        utc = datetime(2026, 10, 31, 9, 6, tzinfo=UTC)
        for freq in (102.3, 102.4, 102.5):
            c.record(_obs(freq, 0x3211, utc, mono=100.0))
        result = c.consensus(monotonic_now=101.0)
        assert len(c.tracks) == 3  # Keep all captures available for diagnostics.
        assert result.n_sources == 1
        assert result.trust_level == TrustLevel.LOW

    def test_repeated_pi_does_not_outvote_two_other_programmes(self):
        c = TimeConsensus()
        utc = datetime(2026, 10, 31, 9, 6, tzinfo=UTC)
        for freq in (102.2, 102.3, 102.4, 102.5):
            c.record(_obs(freq, 0x3211, utc + timedelta(hours=1), mono=100.0))
        c.record(_obs(98.8, 0x3233, utc, mono=100.0))
        c.record(_obs(94.0, 0x3333, utc, mono=100.0))
        result = c.consensus(monotonic_now=101.0)
        assert result.utc == utc + timedelta(seconds=1)
        assert result.n_sources == 2
        assert result.trust_level == TrustLevel.MEDIUM

    def test_repeated_pi_uses_freshest_observation(self):
        c = TimeConsensus()
        utc = datetime(2026, 10, 31, 9, 6, tzinfo=UTC)
        c.record(_obs(102.3, 0x3211, utc, mono=100.0))
        c.record(_obs(102.4, 0x3211, utc + timedelta(minutes=1), mono=160.0))
        result = c.consensus(monotonic_now=161.0)
        assert result.utc == utc + timedelta(seconds=61)
        assert result.contributing_freqs_mhz == [102.4]

    def test_changed_pi_on_one_channel_does_not_create_multiple_sources(self):
        c = TimeConsensus()
        utc = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
        for second, pi in enumerate((0x1111, 0x2222, 0x3333)):
            c.record(_obs(102.4, pi, utc + timedelta(seconds=second), mono=100.0 + second))
        result = c.consensus(monotonic_now=103.0)
        assert len(c.tracks) == 3
        assert result.utc == utc + timedelta(seconds=3)
        assert result.n_sources == 1
        assert result.trust_level == TrustLevel.LOW

    def test_confirming_pi_replaces_unconfirmed_vote_in_the_same_channel_bin(self):
        c = TimeConsensus()
        utc = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
        c.record(_obs(102.38, None, utc + timedelta(hours=1), mono=100.0))
        c.record(_obs(102.42, 0x3211, utc + timedelta(seconds=1), mono=101.0))
        result = c.consensus(monotonic_now=102.0)
        assert len(c.tracks) == 2
        assert result.utc == utc + timedelta(seconds=2)
        assert result.n_sources == 1
        assert result.contributing_freqs_mhz == [102.42]

    def test_fresher_unconfirmed_pi_replaces_old_confirmed_clock(self):
        c = TimeConsensus()
        utc = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
        c.record(_obs(102.4, 0x3211, utc + timedelta(hours=1), mono=100.0))
        c.record(_obs(102.4, None, utc + timedelta(seconds=1), mono=101.0))
        result = c.consensus(monotonic_now=102.0)
        assert result.utc == utc + timedelta(seconds=2)
        assert result.n_sources == 1
        assert result.trust_level == TrustLevel.LOW

    def test_channel_representative_uses_receipt_instead_of_decode_completion(self):
        c = TimeConsensus()
        utc = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
        c.record(
            StationObservation(
                102.4e6,
                0x1111,
                utc + timedelta(hours=1),
                150.0,
                rx_monotonic_ns=100_000_000_000,
            )
        )
        c.record(_obs(102.4, 0x2222, utc + timedelta(seconds=20), mono=120.0))
        result = c.consensus(monotonic_now=151.0)
        assert result.utc == utc + timedelta(seconds=51)
        assert result.n_sources == 1

    def test_channel_deduplication_precedes_programme_deduplication(self):
        c = TimeConsensus()
        utc = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
        c.record(_obs(102.4, 0x1111, utc + timedelta(hours=1), mono=100.0))
        c.record(_obs(102.4, 0x2222, utc + timedelta(seconds=10), mono=110.0))
        c.record(_obs(98.8, 0x2222, utc + timedelta(seconds=20), mono=120.0))
        result = c.consensus(monotonic_now=121.0)
        assert len(c.tracks) == 3
        assert result.utc == utc + timedelta(seconds=21)
        assert result.n_sources == 1
        assert result.contributing_freqs_mhz == [98.8]

    def test_unconfirmed_pi_cannot_raise_trust(self):
        c = TimeConsensus()
        utc = datetime(2026, 10, 31, 9, 6, tzinfo=UTC)
        for freq in (92.0, 98.8, 102.4):
            c.record(_obs(freq, None, utc, mono=100.0))
        result = c.consensus(monotonic_now=101.0)
        assert result.n_sources == 3
        assert result.trust_level == TrustLevel.LOW

    def test_high_trust_requires_three_confirmed_pi(self):
        c = TimeConsensus()
        utc = datetime(2026, 10, 31, 9, 6, tzinfo=UTC)
        c.record(_obs(92.0, 0x1111, utc, mono=100.0))
        c.record(_obs(98.8, 0x2222, utc, mono=100.0))
        c.record(_obs(102.4, None, utc, mono=100.0))
        result = c.consensus(monotonic_now=101.0)
        assert result.n_sources == 3
        assert result.trust_level == TrustLevel.MEDIUM

    def test_sub_second_consensus_learns_station_latency_and_back_propagates(self):
        c = TimeConsensus()
        base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        mono_origin_ns = 10_000_000_000
        station_latencies = {
            (92.0, 0x3201): 5_000_000_000,
            (98.3, 0x4321): 8_000_000_000,
            (106.8, 0x5555): 11_000_000_000,
        }
        for minute in range(3):
            for (freq, pi), latency_ns in station_latencies.items():
                c.record(
                    _timestamped_obs(
                        freq,
                        pi,
                        base + timedelta(minutes=minute),
                        latency_ns,
                        mono_origin_ns,
                        base,
                    )
                )

        estimate = c.sub_second_consensus(
            monotonic_now_ns=mono_origin_ns + 180_000_000_000,
            wall_now_utc=base + timedelta(minutes=3),
        )

        assert isinstance(estimate, SubSecondEstimate)
        assert estimate.utc == base + timedelta(minutes=2)
        assert estimate.precision_ms == 0.0
        assert estimate.station_count == 3
        for track in c.tracks.values():
            assert track.tx_latency_ns in station_latencies.values()
            assert all(obs.tx_latency_ns == track.tx_latency_ns for obs in track.observations)
            assert all(
                obs.clock_time is not None and obs.clock_time.tx_latency_ns == track.tx_latency_ns
                for obs in track.observations
            )

    def test_sub_second_consensus_requires_two_estimated_station_latencies(self):
        c = TimeConsensus()
        base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        for minute in range(3):
            c.record(
                _timestamped_obs(
                    92.0,
                    0x3201,
                    base + timedelta(minutes=minute),
                    5_000_000_000,
                    10_000_000_000,
                    base,
                )
            )
        assert (
            c.sub_second_consensus(
                monotonic_now_ns=190_000_000_000,
                wall_now_utc=base + timedelta(minutes=3),
            )
            is None
        )

    def test_sub_second_consensus_default_clock_and_edge_branches(self, monkeypatch):
        c = TimeConsensus()
        base = datetime(2026, 5, 17, 12, 0)
        mono_origin_ns = 20_000_000_000
        mono_now_ns = mono_origin_ns + 180_000_000_000
        wall_now = base + timedelta(minutes=3)
        monkeypatch.setattr("rdsclock.time_consensus.time.monotonic_ns", lambda: mono_now_ns)
        monkeypatch.setattr(
            "rdsclock.time_consensus.time.time_ns",
            lambda: int(wall_now.replace(tzinfo=UTC).timestamp() * 1_000_000_000),
        )

        # One incomplete track covers the "no learned latency yet" branch.
        c.record(
            StationObservation(
                freq_hz=88.0e6,
                pi=0x1111,
                ct_utc=base.replace(tzinfo=UTC),
                received_monotonic=20.0,
            )
        )
        for minute in range(3):
            for freq, pi, latency_ns in [
                (92.0, 0x3201, 5_000_000_000),
                (98.3, 0x4321, 7_000_000_000),
            ]:
                declared = base + timedelta(minutes=minute)
                rx_monotonic_ns = mono_origin_ns + minute * 60_000_000_000 + latency_ns
                obs = StationObservation(
                    freq_hz=freq * 1e6,
                    pi=pi,
                    ct_utc=declared,
                    received_monotonic=rx_monotonic_ns / 1_000_000_000,
                    rx_monotonic_ns=rx_monotonic_ns,
                )
                c.record(obs)
                obs.received_wall_utc = None

        estimate = c.sub_second_consensus()

        assert estimate is not None
        assert estimate.station_count == 2

        empty_rx_track = StationTrack(freq_hz=99.9e6, pi=0x9999, tx_latency_ns=1)
        empty_rx_track.add_observation(_obs(99.9, 0x9999, base.replace(tzinfo=UTC), mono=1.0))
        c.tracks[(99_900_000, 0x9999)] = empty_rx_track
        estimates = c._sub_second_station_estimates(mono_now_ns, 0)
        assert len(estimates) == 2

    def test_sub_second_consensus_rejects_stale_or_duplicate_sources(self):
        base = datetime(2026, 10, 31, 9, 6, tzinfo=UTC)
        origin = 10_000_000_000
        c = TimeConsensus()
        for minute in range(3):
            for freq in (102.3, 102.4, 102.5):
                c.record(
                    _timestamped_obs(
                        freq, 0x3211, base + timedelta(minutes=minute), 5_000_000_000, origin, base
                    )
                )
        assert c.sub_second_consensus(origin + 180_000_000_000, base + timedelta(minutes=3)) is None

        for minute in range(3):
            c.record(
                _timestamped_obs(
                    98.8, 0x3233, base + timedelta(minutes=minute), 5_000_000_000, origin, base
                )
            )
        assert (
            c.sub_second_consensus(origin + 180_000_000_000, base + timedelta(minutes=3))
            is not None
        )
        assert (
            c.sub_second_consensus(origin + 600_000_000_000, base + timedelta(minutes=10)) is None
        )

    def test_station_observation_normalizes_naive_received_wall(self):
        obs = StationObservation(
            freq_hz=92e6,
            pi=0x3201,
            ct_utc=datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
            received_monotonic=1.0,
            received_wall_utc=datetime(2026, 5, 17, 12, 0),
        )
        assert obs.received_wall_utc is not None
        assert obs.received_wall_utc.tzinfo is UTC


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


class TestParallelReceivers:
    """Several receivers report to one consensus, in whatever order they finish."""

    @staticmethod
    def _heard(pi: int, mono: float, receiver: str, freq_mhz: float = 98.8):
        utc = datetime(2026, 5, 17, 12, 0, tzinfo=UTC) + timedelta(seconds=mono)
        return replace(_obs(freq_mhz, pi, utc, mono), receiver=receiver)

    def test_an_older_reception_reported_later_does_not_age_the_track(self):
        c = TimeConsensus(mission_precision_s=60)
        track = c.record(self._heard(0x1001, 160.0, "a:1"))
        c.record(self._heard(0x1001, 100.0, "b:1"))  # finished later, heard earlier
        assert [o.reception_monotonic for o in track.observations] == [100.0, 160.0]
        assert track.last.reception_monotonic == 160.0
        result = c.consensus(monotonic_now=175.0)
        assert result.median_age_s == 15.0
        assert result.trust_level == TrustLevel.LOW  # age 75 would be STALE

    def test_receivers_hearing_different_transmitters_on_one_channel_count_twice(self):
        c = TimeConsensus()
        c.record(self._heard(0x1001, 100.0, "a:1"))
        c.record(self._heard(0x1002, 101.0, "b:1"))
        result = c.consensus(monotonic_now=102.0)
        assert result.n_sources == 2
        assert result.trust_level == TrustLevel.MEDIUM

    def test_one_receiver_still_gives_one_vote_per_channel(self):
        c = TimeConsensus()
        c.record(self._heard(0x1001, 100.0, "a:1"))
        c.record(self._heard(0x1002, 101.0, "a:1"))  # the channel changed PI
        assert c.consensus(monotonic_now=102.0).n_sources == 1

    def test_a_channel_that_changed_pi_for_one_receiver_only(self):
        c = TimeConsensus()
        c.record(self._heard(0x1001, 100.0, "a:1"))
        c.record(self._heard(0x1001, 105.0, "b:1"))
        c.record(self._heard(0x1002, 110.0, "a:1"))
        # a:1 now hears 0x1002 and b:1 still 0x1001.
        assert c.consensus(monotonic_now=111.0).n_sources == 2

    def test_one_transmitter_heard_by_two_receivers_counts_once(self):
        c = TimeConsensus()
        c.record(self._heard(0x1001, 100.0, "a:1"))
        c.record(self._heard(0x1001, 101.0, "b:1"))
        assert c.consensus(monotonic_now=102.0).n_sources == 1

    def test_an_expired_reception_does_not_keep_a_receivers_vote(self):
        # At 1000: A heard P at 0 (expired), B heard P at 990 and then an
        # unconfirmed PI at 995. P is active only through B, and B's vote is
        # now the unconfirmed PI; A's old reception must not revive P.
        c = TimeConsensus()
        c.record(self._heard(0x1001, 0.0, "a:1"))
        c.record(self._heard(0x1001, 990.0, "b:1"))
        c.record(replace(self._heard(0x1001, 995.0, "b:1"), pi=None))
        c.record(self._heard(0x2002, 999.0, "c:1", freq_mhz=102.4))
        result = c.consensus(monotonic_now=1000.0)
        assert result.n_sources == 2  # the unconfirmed PI and Q
        assert result.trust_level == TrustLevel.LOW  # not MEDIUM: one confirmed PI
        statuses = {track.pi: track.consensus_status for track in c.tracks.values()}
        assert statuses == {0x1001: None, None: "contributing", 0x2002: "contributing"}

    def test_a_fresh_reception_by_another_receiver_still_counts(self):
        c = TimeConsensus()
        c.record(self._heard(0x1001, 0.0, "a:1"))
        c.record(self._heard(0x1001, 990.0, "b:1"))
        c.record(self._heard(0x2002, 999.0, "c:1", freq_mhz=102.4))
        result = c.consensus(monotonic_now=1000.0)
        assert result.n_sources == 2
        assert result.trust_level == TrustLevel.MEDIUM

    def test_an_expired_reception_does_not_keep_a_sub_second_vote(self):
        base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        origin_ns = 10_000_000_000

        def timestamped(freq_mhz, pi, minute, latency_ns, receiver):
            obs = _timestamped_obs(
                freq_mhz, pi, base + timedelta(minutes=minute), latency_ns, origin_ns, base
            )
            return replace(obs, receiver=receiver)

        def estimate(unconfirmed_too: bool):
            c = TimeConsensus()
            for minute in range(3):  # A's receptions of P expire by minute 8
                c.record(timestamped(92.0, 0x3201, minute, 5_000_000_000, "a:1"))
            for minute in (5, 6, 7):
                c.record(timestamped(98.3, 0x4321, minute, 8_000_000_000, "c:1"))
            c.record(timestamped(92.0, 0x3201, 7, 5_000_000_000, "b:1"))
            if unconfirmed_too:
                later_s = (origin_ns + 450_000_000_000) / 1e9  # minute 7.5
                c.record(
                    replace(
                        _obs(92.0, None, base + timedelta(minutes=7.5), later_s), receiver="b:1"
                    )
                )
            return c.sub_second_consensus(
                monotonic_now_ns=origin_ns + 480_000_000_000,
                wall_now_utc=base + timedelta(minutes=8),
            )

        assert estimate(unconfirmed_too=False).station_count == 2  # P counts through B
        assert estimate(unconfirmed_too=True) is None  # B now votes for the unconfirmed PI

    def test_each_transmitter_on_a_shared_frequency_is_classified(self):
        # A hears a wrong clock on 98.8, B the right one there, C sets the majority.
        c = TimeConsensus()
        c.record(self._heard(0x1001, 100.0, "a:1"))
        wrong = self._heard(0x1002, 100.0, "b:1")
        c.record(replace(wrong, ct_utc=wrong.ct_utc + timedelta(hours=1)))
        c.record(self._heard(0x1003, 100.0, "c:1", freq_mhz=102.4))
        result = c.consensus(monotonic_now=101.0)
        assert result.contributing_freqs_mhz == [98.8, 102.4]
        assert result.outlier_freqs_mhz == [98.8]
        statuses = {track.pi: track.consensus_status for track in c.tracks.values()}
        assert statuses == {0x1001: "contributing", 0x1002: "outlier", 0x1003: "contributing"}
        # Every evaluation classifies afresh: an expired track is no longer counted.
        c.consensus(monotonic_now=1000.0)
        assert {track.consensus_status for track in c.tracks.values()} == {None}
