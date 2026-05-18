"""Regression coverage for a real Warsaw FM RDS capture."""

from pathlib import Path

import pytest

from rdsclock import dsp
from rdsclock.decoder import decode_file, decode_iq

FIXTURE = Path(__file__).parent / "fixtures" / "trojka_98p8_6s_250k_u8.iq"
JEDYNKA_BASELINE = (
    Path(__file__).resolve().parent.parent
    / "eter"
    / "baseline-20260518-035918"
    / "live-102.4MHz-300s.iq"
)


@pytest.mark.integration
def test_trojka_98p8_real_iq_decodes_groups_and_polskie_radio_pi():
    result = decode_file(str(FIXTURE), fs=250_000, fmt="u8")

    assert result.n_groups_clean + result.n_groups_corrected == result.n_groups
    assert result.n_groups_clean + result.n_groups_corrected >= 30
    assert result.n_groups_corrected < result.n_groups_clean * 0.5
    assert result.info.pi is not None
    assert (result.info.pi & 0xFF00) == 0x3200


@pytest.mark.integration
@pytest.mark.slow
def test_jedynka_baseline_rx_monotonic_timestamps_increase_by_about_a_minute():
    iq = dsp.read_iq_complex64(str(JEDYNKA_BASELINE))
    result = decode_iq(iq, fs=250_000, capture_start_monotonic_ns=1_000_000_000)

    assert len(result.info.clock_times) == 3
    rx_values = [ct.rx_monotonic_ns for ct in result.info.clock_times]
    assert all(rx is not None for rx in rx_values)
    assert rx_values == sorted(rx_values)

    deltas = [b - a for a, b in zip(rx_values[:-1], rx_values[1:], strict=True)]
    for delta in deltas:
        nearest_minute_multiple = round(delta / 60_000_000_000) * 60_000_000_000
        assert nearest_minute_multiple >= 60_000_000_000
        assert abs(delta - nearest_minute_multiple) <= 5_000_000_000
