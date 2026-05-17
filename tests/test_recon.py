"""Unit tests for the helpers in :mod:`rdsclock.recon`.

The live ``run_recon`` loop and ``quick_scan_band`` open ``rtl_tcp``
and are covered separately by ``tests/test_real_recordings.py`` when
the hardware is available. The offline replay is covered by
``tests/test_recon_offline.py``. Here we hit the small functional
units that can be tested without any SDR.
"""

import time
from datetime import UTC, datetime

import pytest

from rdsclock.recon import (
    ReconConfig,
    StationCandidate,
    _parse_freq_from_filename,
    rank_candidates,
    render_status,
)
from rdsclock.time_consensus import (
    StationObservation,
    TimeConsensus,
)


class TestParseFreqFromFilename:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("fm_098.30_MHz.iq", 98.30),
            ("live_95.5.iq", 95.5),
            ("fm_087.80_MHz.iq", 87.80),
            ("c01_2343_089.00MHz.iq", 89.00),
            ("scan_102.4_capture.iq", 102.4),
        ],
    )
    def test_extracts_mhz(self, name, expected):
        assert _parse_freq_from_filename(name) == pytest.approx(expected)

    @pytest.mark.parametrize("name", ["random.iq", "noname", "fm_iq"])
    def test_returns_none_on_no_match(self, name):
        assert _parse_freq_from_filename(name) is None


class TestRankCandidates:
    def test_ct_beats_no_ct(self):
        a = StationCandidate(freq_hz=90e6, rssi_db=-20, n_groups=10, has_ct=False, pi=1)
        b = StationCandidate(freq_hz=91e6, rssi_db=-30, n_groups=5, has_ct=True, pi=2)
        ranked = rank_candidates([a, b])
        assert ranked[0] is b

    def test_more_groups_beats_fewer_when_ct_equal(self):
        a = StationCandidate(freq_hz=90e6, rssi_db=-10, n_groups=100, has_ct=False, pi=1)
        b = StationCandidate(freq_hz=91e6, rssi_db=-30, n_groups=200, has_ct=False, pi=2)
        ranked = rank_candidates([a, b])
        assert ranked[0] is b

    def test_rssi_breaks_ties(self):
        a = StationCandidate(freq_hz=90e6, rssi_db=-30, n_groups=10, has_ct=True, pi=1)
        b = StationCandidate(freq_hz=91e6, rssi_db=-10, n_groups=10, has_ct=True, pi=2)
        ranked = rank_candidates([a, b])
        assert ranked[0] is b

    def test_empty_input(self):
        assert rank_candidates([]) == []


class TestReconConfigDefaults:
    def test_defaults_are_sensible(self):
        cfg = ReconConfig()
        assert cfg.band_start_mhz < cfg.band_end_mhz
        assert cfg.scan_step_mhz > 0
        assert cfg.scan_dwell_s > 0
        assert cfg.dwell_s > 0
        assert cfg.mission_precision_s > 0
        assert cfg.sample_rate == 250_000
        assert cfg.iterations is None  # run forever by default


class TestRenderStatus:
    def _populated_consensus(self) -> TimeConsensus:
        c = TimeConsensus()
        utc = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        mono = time.monotonic()
        for freq_mhz, pi in [(92.0, 0x3001), (98.3, 0x3002), (106.8, 0x3003)]:
            c.record(
                StationObservation(
                    freq_hz=freq_mhz * 1e6,
                    pi=pi,
                    ct_utc=utc,
                    received_monotonic=mono,
                )
            )
        return c

    def test_render_contains_consensus_line(self):
        consensus = self._populated_consensus()
        watchlist = [
            StationCandidate(
                freq_hz=92.0e6, rssi_db=-12.0, n_groups=120, has_ct=True, pi=0x3001, ps_name="A"
            ),
        ]
        text = render_status(consensus, watchlist, next_rescan_in_s=240.0)
        assert "rdsclock recon" in text
        assert "CONSENSUS:" in text
        assert "SYSTEM:" in text
        assert "Watchlist (1)" in text
        assert "Next rescan in: 240s" in text

    def test_render_with_empty_watchlist(self):
        consensus = TimeConsensus()
        text = render_status(consensus, [], next_rescan_in_s=0.0)
        assert "Watchlist (0)" in text
        assert "(empty — acquisition in progress)" in text

    def test_render_with_explicit_system_now(self):
        consensus = self._populated_consensus()
        sys_now = datetime(2026, 5, 17, 12, 0, 5, tzinfo=UTC)
        text = render_status(consensus, [], next_rescan_in_s=600.0, system_now=sys_now)
        assert "2026-05-17 12:00:05" in text
