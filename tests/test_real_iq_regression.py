"""Regression coverage for a real Warsaw FM RDS capture."""

from pathlib import Path

import pytest

from rdsclock.decoder import decode_file

FIXTURE = Path(__file__).parent / "fixtures" / "trojka_98p8_6s_250k_u8.iq"


@pytest.mark.integration
def test_trojka_98p8_real_iq_decodes_groups_and_polskie_radio_pi():
    result = decode_file(str(FIXTURE), fs=250_000, fmt="u8")

    assert result.n_groups >= 10
    assert result.info.pi is not None
    assert (result.info.pi & 0xFF00) == 0x3200
