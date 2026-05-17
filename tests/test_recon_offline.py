"""Test offline recon mode: synthetic recordings from several stations -> consensus."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from rdsclock import dsp
from rdsclock.rds_groups import encode_group_4a, encode_ps_groups
from rdsclock.recon import ReconConfig, run_recon_offline
from rdsclock.synth import synthesize_fm_iq
from rdsclock.time_consensus import TrustLevel


def _make_recording(
    path: Path, freq_mhz: float, ct_utc: datetime, pi: int, ps: str, snr_db: float = None
) -> None:
    """Generate 1.5s of synthetic IQ with known CT and save it to a file."""
    groups = encode_ps_groups(pi=pi, ps_name=ps) + [encode_group_4a(pi=pi, clock_time_local=ct_utc)]
    iq = synthesize_fm_iq(
        groups, duration_s=1.5, fs=250_000, snr_db=snr_db, rng=np.random.default_rng(0)
    )
    dsp.write_iq_complex64(iq, str(path))


@pytest.mark.slow
def test_recon_offline_three_stations_consensus(tmp_path):
    """3 synthetic stations with nearby CT -> HIGH consensus, no outliers."""
    base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    _make_recording(tmp_path / "fm_092.00_MHz.iq", 92.0, base, 0x3001, "STN_A   ")
    _make_recording(
        tmp_path / "fm_098.30_MHz.iq", 98.3, base + timedelta(seconds=10), 0x3002, "STN_B   "
    )
    _make_recording(
        tmp_path / "fm_106.80_MHz.iq", 106.8, base + timedelta(seconds=-5), 0x3003, "STN_C   "
    )

    cfg = ReconConfig(mission_precision_s=60.0)
    consensus = run_recon_offline(cfg, str(tmp_path), on_status=lambda _: None)
    result = consensus.consensus()
    assert result.utc is not None
    assert result.n_sources == 3
    # 3 agreeing stations -> HIGH
    assert result.trust_level == TrustLevel.HIGH
    assert not result.outlier_freqs_mhz
    # Consensus time close to base (median = base + 0s, tolerance 60s)
    delta = abs((result.utc - base).total_seconds())
    assert delta < 60


@pytest.mark.slow
def test_recon_offline_detects_outlier(tmp_path):
    """2 agreeing + 1 "lying" station -> outlier detected and median still correct."""
    base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    _make_recording(tmp_path / "fm_092.00_MHz.iq", 92.0, base, 0x3001, "STN_A   ")
    _make_recording(
        tmp_path / "fm_098.30_MHz.iq", 98.3, base + timedelta(seconds=3), 0x3002, "STN_B   "
    )
    # Station clock 10 min ahead
    _make_recording(
        tmp_path / "fm_106.80_MHz.iq", 106.8, base + timedelta(minutes=10), 0x3003, "EVIL_STN"
    )

    cfg = ReconConfig()
    consensus = run_recon_offline(cfg, str(tmp_path), on_status=lambda _: None)
    result = consensus.consensus()
    assert result.utc is not None
    # Consensus from 2 good stations
    assert result.n_sources == 2
    # 106.8 marked as an outlier
    assert 106.8 in result.outlier_freqs_mhz
    # The median should stay close to base, not the outlier value
    delta = abs((result.utc - base).total_seconds())
    assert delta < 60


@pytest.mark.slow
def test_recon_offline_simulates_gps_synced_stations(tmp_path):
    """Simulate 3 foreign stations (DE/UK/CZ-like) - all GPS-synced to
    second-level accuracy. This shows the pipeline WOULD give an operator HIGH
    trust in a real deployment if it hit GPS-synced FM stations."""
    base = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    # 3 stations, all within <10s of each other (typical for GPS-disciplined setups)
    _make_recording(tmp_path / "fm_089.00_MHz.iq", 89.0, base, 0xD001, "BR-EINS ")  # DE-like
    _make_recording(
        tmp_path / "fm_098.30_MHz.iq", 98.3, base + timedelta(seconds=3), 0xC479, "BBC R2  "
    )  # UK-like (BBC Radio 2)
    _make_recording(
        tmp_path / "fm_104.30_MHz.iq", 104.3, base + timedelta(seconds=-5), 0xD420, "CRO 1   "
    )  # CZ-like

    cfg = ReconConfig(mission_precision_s=60.0)
    consensus = run_recon_offline(cfg, str(tmp_path), on_status=lambda _: None)
    result = consensus.consensus()

    assert result.utc is not None
    # 3 agreeing stations -> HIGH trust
    assert result.trust_level == TrustLevel.HIGH, (
        f"expected HIGH, got {result.trust_level} (notes={result.notes})"
    )
    assert result.n_sources == 3
    assert not result.outlier_freqs_mhz
    # All 3 stations contributed to the consensus
    assert sorted(result.contributing_freqs_mhz) == [89.0, 98.3, 104.3]
    # Median close to base (max 30s)
    delta_s = abs((result.utc - base).total_seconds())
    assert delta_s < 30, f"median differs from base by {delta_s:.1f}s"
    # Low uncertainty
    assert result.uncertainty_s < 30


@pytest.mark.slow
def test_recon_offline_no_ct_files(tmp_path):
    """Files without CT -> STALE consensus with 0 sources."""
    # Files with PS only, without CT (encode_ps_groups without encode_group_4a)
    from rdsclock.synth import synthesize_fm_iq

    groups = encode_ps_groups(pi=0x3001, ps_name="NO_CT_PS")
    iq = synthesize_fm_iq(
        groups, duration_s=1.5, fs=250_000, snr_db=None, rng=np.random.default_rng(0)
    )
    dsp.write_iq_complex64(iq, str(tmp_path / "fm_092.00_MHz.iq"))

    cfg = ReconConfig()
    consensus = run_recon_offline(cfg, str(tmp_path), on_status=lambda _: None)
    result = consensus.consensus()
    # No CT at all
    assert result.utc is None
    assert result.n_sources == 0
    assert result.trust_level == TrustLevel.STALE
