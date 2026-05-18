"""Pipeline delay calibration for receive timestamping."""

from datetime import UTC, datetime

from rdsclock import dsp
from rdsclock.decoder import decode_iq
from rdsclock.rds_groups import encode_group_4a, encode_ps_groups
from rdsclock.synth import DEFAULT_FS, synthesize_fm_iq


def test_pipeline_group_delay_constant_matches_synthetic_group_start():
    ct_utc = datetime(2026, 5, 16, 14, 30, tzinfo=UTC)
    groups = [encode_group_4a(pi=0xCAFE, clock_time_local=ct_utc)] + encode_ps_groups(
        pi=0xCAFE,
        ps_name="TESTSDR ",
    )
    iq = synthesize_fm_iq(groups, duration_s=1.2, fs=DEFAULT_FS, snr_db=None)

    result = decode_iq(iq, fs=DEFAULT_FS)

    assert result.info.latest_clock is not None
    decoded_start_bit = result.group_bit_positions[0]
    decoded_start_sample = round(decoded_start_bit / dsp.RDS_SYMBOL_RATE * DEFAULT_FS)
    measured_delay_samples = decoded_start_sample
    assert abs(measured_delay_samples - 632) <= 100
    assert -measured_delay_samples * 4_000 == dsp.PIPELINE_GROUP_DELAY_NS
