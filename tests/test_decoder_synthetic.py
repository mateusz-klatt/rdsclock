"""KEY TEST: clock embedded in synthetic IQ -> decoder -> the same clock.

Here we prove that the full pipeline (modulator -> channel -> demodulator)
works end to end. No real SDR and no real station.
"""

from datetime import UTC, datetime, timedelta, timezone

import numpy as np
import pytest

from rdsclock import decoder as decoder_module
from rdsclock import dsp
from rdsclock.decoder import decode_iq
from rdsclock.rds_groups import encode_group_4a, encode_ps_groups
from rdsclock.synth import DEFAULT_FS, synthesize_fm_iq

CET = timezone(timedelta(minutes=120))  # +02:00


def _build_groups(ct_time: datetime, ps_name: str = "TESTSDR ", pi: int = 0xCAFE):
    """Standard test mix: PS (4 groups 0A) + CT (1 group 4A) - in rotation."""
    ps_groups = encode_ps_groups(pi=pi, ps_name=ps_name)
    ct_group = encode_group_4a(pi=pi, clock_time_local=ct_time)
    # Return them interleaved: 0A, 0A, 0A, 0A, 4A
    return ps_groups + [ct_group]


@pytest.mark.parametrize("snr_db", [None, 30.0])
def test_roundtrip_high_snr(snr_db):
    expected = datetime(2026, 5, 16, 14, 30, tzinfo=UTC)
    groups = _build_groups(expected)
    iq = synthesize_fm_iq(groups, duration_s=2.0, snr_db=snr_db, rng=np.random.default_rng(0))
    result = decode_iq(iq, fs=DEFAULT_FS)

    assert result.n_groups > 0, f"no groups at SNR={snr_db}"
    assert result.info.latest_clock is not None, f"no CT at SNR={snr_db}"
    assert result.info.latest_clock.utc == expected
    assert result.info.ps_name == "TESTSDR"
    assert result.info.pi == 0xCAFE


def test_roundtrip_with_local_offset():
    local = datetime(2026, 5, 16, 16, 30, tzinfo=CET)
    groups = _build_groups(local)
    iq = synthesize_fm_iq(groups, duration_s=2.0, snr_db=25.0, rng=np.random.default_rng(0))
    result = decode_iq(iq, fs=DEFAULT_FS)
    ct = result.info.latest_clock
    assert ct is not None
    assert ct.utc == local.astimezone(UTC)
    assert ct.local_offset_minutes == 120
    assert ct.local == local


def test_roundtrip_with_carrier_offset():
    """The Costas loop should tolerate an offset of roughly a few hundred Hz."""
    expected = datetime(2026, 5, 16, 14, 30, tzinfo=UTC)
    groups = _build_groups(expected)
    iq = synthesize_fm_iq(
        groups, duration_s=2.0, snr_db=25.0, carrier_offset_hz=200.0, rng=np.random.default_rng(0)
    )
    result = decode_iq(iq, fs=DEFAULT_FS)
    ct = result.info.latest_clock
    assert ct is not None
    assert ct.utc == expected


def test_pipeline_returns_diagnostics():
    expected = datetime(2026, 5, 16, 14, 30, tzinfo=UTC)
    groups = _build_groups(expected)
    iq = synthesize_fm_iq(groups, duration_s=1.0, snr_db=25.0, rng=np.random.default_rng(0))
    result = decode_iq(iq, fs=DEFAULT_FS)
    assert result.n_bits > 0
    assert 0 <= result.symbol_offset < 16
    assert len(result.group_bit_positions) == result.n_groups


def test_decode_iq_attaches_rx_monotonic_when_capture_anchor_is_known():
    expected = datetime(2026, 5, 16, 14, 30, tzinfo=UTC)
    groups = _build_groups(expected)
    iq = synthesize_fm_iq(groups, duration_s=2.0, snr_db=None, rng=np.random.default_rng(0))
    result = decode_iq(iq, fs=DEFAULT_FS, capture_start_monotonic_ns=1_000_000_000)

    assert result.info.clock_times
    rx_values = [ct.rx_monotonic_ns for ct in result.info.clock_times]
    assert all(rx is not None for rx in rx_values)
    assert rx_values == sorted(rx_values)


def test_decoder_position_and_timestamp_helpers():
    assert decoder_module._positions_on_capture_axis(1000, [10, 20], False) == [10, 20]
    assert decoder_module._positions_on_capture_axis(1000, [10], True) == [886]

    drift = dsp.BitRateDrift(
        bit_rate_hz=dsp.RDS_SYMBOL_RATE,
        pilot_hz=19_000.0,
        pilot_mad_hz=0.0,
        confident=True,
    )
    values = decoder_module._rx_monotonic_ns_by_group(1_000, [0], drift)
    assert values == [1_000 + int(104 / dsp.RDS_SYMBOL_RATE * 1e9) + dsp.PIPELINE_GROUP_DELAY_NS]
    assert decoder_module._rx_monotonic_ns_by_group(None, [0], drift) is None


@pytest.mark.slow
def test_robust_at_many_times():
    """Sweep across 24h - every few hours. High SNR to keep it deterministic."""
    base = datetime(2026, 5, 16, 0, 0, tzinfo=UTC)
    failures = []
    for hours in range(0, 24, 3):
        for minutes in [0, 17, 33, 47]:
            t = base + timedelta(hours=hours, minutes=minutes)
            groups = _build_groups(t)
            iq = synthesize_fm_iq(
                groups, duration_s=2.0, snr_db=None, rng=np.random.default_rng(42)
            )
            result = decode_iq(iq, fs=DEFAULT_FS)
            ct = result.info.latest_clock
            if ct is None or ct.utc != t:
                failures.append((t, ct.utc if ct else None))
    assert not failures, f"failures: {failures[:5]}"
