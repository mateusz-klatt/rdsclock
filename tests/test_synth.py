"""Tests for synthetic FM + RDS signal synthesis."""

import numpy as np
import pytest

from rdsclock.rds_blocks import differential_decode, find_groups_in_bitstream
from rdsclock.rds_groups import encode_ps_groups, parse_groups
from rdsclock.synth import (
    DEFAULT_INTERMEDIATE_FS,
    RDS_CARRIER,
    add_awgn,
    biphase_symbols,
    fm_modulate,
    make_mpx,
    modulate_rds,
    rds_baseband,
    rds_groups_to_bits,
    synthesize_fm_iq,
)


class TestBitstreamSynth:
    def test_biphase_symbols_use_half_bit_chips(self):
        bits = np.array([1, 0], dtype=np.uint8)
        shaped = biphase_symbols(bits, samples_per_bit=4)
        np.testing.assert_array_equal(shaped, [1, 1, -1, -1, -1, -1, 1, 1])

    def test_biphase_symbols_require_even_samples_per_bit(self):
        with pytest.raises(ValueError, match="even"):
            biphase_symbols(np.array([1], dtype=np.uint8), samples_per_bit=3)

    def test_known_group_present(self):
        # Single group -> bits -> find it again
        ps = encode_ps_groups(pi=0xCAFE, ps_name="HELLO   ")
        bits = rds_groups_to_bits(ps, differential=False)  # no differential coding for simplicity
        groups = find_groups_in_bitstream(bits)
        assert len(groups) >= len(ps)
        info = parse_groups(groups[: len(ps)] + groups[: len(ps)])
        assert info.pi == 0xCAFE
        assert info.ps_name == "HELLO"

    def test_with_differential_encoding(self):
        ps = encode_ps_groups(pi=0xCAFE, ps_name="HELLO   ")
        tx_bits = rds_groups_to_bits(ps, differential=True)
        # The receiver differentially decodes it back
        data_bits = differential_decode(tx_bits)
        groups = find_groups_in_bitstream(data_bits)
        assert len(groups) >= len(ps)


class TestSpectra:
    def test_rds_baseband_below_symbol_rate(self):
        # Bits -> baseband. Biphase energy should stay around the chip-rate band.
        bits = np.array([0, 1, 0, 1, 0, 1, 0, 1] * 200, dtype=np.uint8)
        bb = rds_baseband(bits, fs=DEFAULT_INTERMEDIATE_FS)
        spec = np.abs(np.fft.rfft(bb))
        freqs = np.fft.rfftfreq(len(bb), 1.0 / DEFAULT_INTERMEDIATE_FS)
        # Most energy below 3 kHz after the transmit shaping filter.
        below = np.sum(spec[freqs < 3000])
        above = np.sum(spec[freqs >= 3000])
        assert below > 2 * above

    def test_rds_baseband_requires_even_integer_samples_per_bit(self):
        bits = np.array([0, 1], dtype=np.uint8)
        with pytest.raises(ValueError, match="even"):
            rds_baseband(bits, fs=RDS_CARRIER / 16, symbol_rate=RDS_CARRIER / 48)

    def test_modulator_peak_near_carrier(self):
        # After BPSK modulation, the band should be around 57 kHz.
        bits = np.array([0, 1, 0, 1] * 500, dtype=np.uint8)
        modulated = modulate_rds(bits, fs=DEFAULT_INTERMEDIATE_FS, carrier=RDS_CARRIER)
        spec = np.abs(np.fft.rfft(modulated))
        freqs = np.fft.rfftfreq(len(modulated), 1.0 / DEFAULT_INTERMEDIATE_FS)
        peak_freq = freqs[np.argmax(spec)]
        assert abs(peak_freq - RDS_CARRIER) < 1000  # +/-1 kHz tolerance

    def test_fm_modulate_unit_amplitude(self):
        mpx = np.zeros(2000, dtype=np.float32)
        mpx[500:1500] = 0.5
        iq = fm_modulate(mpx, fs=DEFAULT_INTERMEDIATE_FS)
        # FM IQ has |IQ|=1 (pure carrier)
        assert np.allclose(np.abs(iq), 1.0, atol=1e-5)

    def test_mpx_contains_pilot(self):
        rng = np.random.default_rng(0)
        rds = (rng.standard_normal(20000) * 0.01).astype(np.float32)
        mpx = make_mpx(rds, fs=DEFAULT_INTERMEDIATE_FS)
        spec = np.abs(np.fft.rfft(mpx))
        freqs = np.fft.rfftfreq(len(mpx), 1.0 / DEFAULT_INTERMEDIATE_FS)
        # Peak near 19 kHz (pilot)
        mask_pilot = (freqs > 18_500) & (freqs < 19_500)
        mask_else_lf = (freqs > 1_000) & (freqs < 17_000)
        pilot_peak = np.max(spec[mask_pilot])
        elsewhere = np.max(spec[mask_else_lf])
        assert pilot_peak > elsewhere


class TestAwgn:
    def test_snr_is_approximately_correct(self):
        rng = np.random.default_rng(0)
        n = 50000
        sig = np.exp(1j * 2 * np.pi * 1000 * np.arange(n) / 100_000).astype(np.complex64)
        for target_db in [20.0, 10.0, 0.0]:
            noisy = add_awgn(sig, snr_db=target_db, rng=rng)
            # Estimate the noise as the difference
            noise = noisy - sig
            signal_power = float(np.mean(np.abs(sig) ** 2))
            noise_power = float(np.mean(np.abs(noise) ** 2))
            measured_db = 10 * np.log10(signal_power / noise_power)
            assert abs(measured_db - target_db) < 1.0


class TestSynthesizeFull:
    def test_returns_correct_length(self):
        groups = encode_ps_groups(pi=0xCAFE, ps_name="TEST    ")
        iq = synthesize_fm_iq(groups, duration_s=0.1, fs=250_000, snr_db=None)
        # 0.1s * 250 kS/s = 25000 samples, +/-200 due to rounding in 228k -> 250k resampling
        assert 24600 < len(iq) < 25400
        # Without noise, |IQ| should stay close to 1 (resampling adds ~5-10% ripple)
        assert np.allclose(np.abs(iq), 1.0, atol=0.15)

    def test_noise_adds_amplitude_variance(self):
        groups = encode_ps_groups(pi=0xCAFE, ps_name="TEST    ")
        iq_clean = synthesize_fm_iq(groups, duration_s=0.05, snr_db=None)
        iq_noisy = synthesize_fm_iq(
            groups, duration_s=0.05, snr_db=5.0, rng=np.random.default_rng(0)
        )
        # noisy has higher amplitude variance than clean (clean = constant 1.0)
        assert np.var(np.abs(iq_noisy)) > np.var(np.abs(iq_clean))
