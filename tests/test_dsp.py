"""DSP smoke tests - checking that filters/demod/Costas behave sensibly on
synthetic inputs."""

import numpy as np

from rdsclock import dsp


class TestIoRoundtrip:
    def test_u8_roundtrip(self, tmp_path):
        # complex64 -> u8 -> complex64 with quantization tolerance ~1/127.
        # Limit the amplitude to avoid clipping (signal * 127.5 + 127.5 in [0..255]).
        rng = np.random.default_rng(0)
        n = 1000
        iq = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * 0.3
        # Hard-clip to a safe range (real-world IQ from rtl_sdr is clipped too).
        iq = np.clip(np.real(iq), -1.0, 1.0) + 1j * np.clip(np.imag(iq), -1.0, 1.0)
        iq = iq.astype(np.complex64)
        path = tmp_path / "test.iq"
        dsp.write_iq_u8(iq, str(path))
        back = dsp.read_iq_u8(str(path))
        assert len(back) == n
        # 1/127.5 ~= 0.0078, small safety margin
        assert np.max(np.abs(back - iq)) < 0.012

    def test_complex64_roundtrip(self, tmp_path):
        rng = np.random.default_rng(0)
        iq = (rng.standard_normal(500) + 1j * rng.standard_normal(500)).astype(np.complex64)
        path = tmp_path / "test.cf32"
        dsp.write_iq_complex64(iq, str(path))
        back = dsp.read_iq_complex64(str(path))
        np.testing.assert_array_equal(back, iq)


class TestFmDemod:
    def test_constant_phase_zero_output(self):
        # Constant amplitude, no rotation -> phase derivative = 0
        iq = np.ones(100, dtype=np.complex64) * 0.5
        out = dsp.fm_demod(iq)
        assert len(out) == 99
        assert np.allclose(out, 0.0, atol=1e-5)

    def test_constant_frequency_constant_output(self):
        fs = 250_000
        f0 = 10_000
        n = np.arange(2000)
        iq = np.exp(1j * 2 * np.pi * f0 * n / fs).astype(np.complex64)
        out = dsp.fm_demod(iq)
        expected = 2 * np.pi * f0 / fs
        # Skip edge samples (end effects)
        assert np.allclose(out[10:-10], expected, atol=1e-4)


class TestChannelFilter:
    def test_passes_low(self):
        fs = 250_000
        n = np.arange(5000)
        iq = np.exp(1j * 2 * np.pi * 1000 * n / fs).astype(np.complex64)
        filtered = dsp.channel_filter(iq, fs)
        # Amplitude should stay close to 1 (after filter delay)
        assert np.mean(np.abs(filtered[500:])) > 0.9

    def test_rejects_high(self):
        fs = 250_000
        n = np.arange(5000)
        # 120 kHz - above the 100 kHz cutoff
        iq = np.exp(1j * 2 * np.pi * 120_000 * n / fs).astype(np.complex64)
        filtered = dsp.channel_filter(iq, fs)
        # Attenuation >20 dB
        assert np.mean(np.abs(filtered[500:])) < 0.1


class TestCostas:
    def test_no_drift_with_aligned_input(self):
        # BPSK +1 (constant) - Costas should not go off the rails
        bits = np.array([1.0] * 100, dtype=np.complex64)
        out = dsp.costas_loop_bpsk(bits, alpha=0.1, beta=0.002)
        # After a short transient, the signal still has constant phase
        assert np.max(np.abs(np.imag(out[20:]))) < 1.0

    def test_python_fallback_matches_public_wrapper(self, monkeypatch):
        samples = np.array([1 + 0j, 0.5 + 0.2j, -1 + 0.1j], dtype=np.complex64)
        monkeypatch.setattr(dsp, "_COSTAS_LOOP_BPSK_NUMBA", None)
        wrapper = dsp.costas_loop_bpsk(samples, alpha=0.1, beta=0.002)
        direct = dsp._costas_loop_bpsk_python(samples, alpha=0.1, beta=0.002)
        np.testing.assert_array_equal(wrapper, direct)

    def test_python_fallback_wraps_phase_both_directions(self):
        positive = np.array([1 + 1j, 1 + 0j], dtype=np.complex64)
        negative = np.array([1 - 1j, 1 + 0j], dtype=np.complex64)
        assert dsp._costas_loop_bpsk_python(positive, alpha=4.0, beta=0.0).shape == positive.shape
        assert dsp._costas_loop_bpsk_python(negative, alpha=4.0, beta=0.0).shape == negative.shape


class TestBiphaseRecovery:
    def test_agc_normalizes_mean_magnitude(self):
        samples = np.array([2 + 0j, 0 + 2j, -2 + 0j], dtype=np.complex64)
        out = dsp.agc(samples)
        assert out.dtype == np.complex64
        assert np.mean(np.abs(out)) == np.float32(1.0)

    def test_matched_filter_aligns_biphase_bits(self):
        shaped = np.array([1, 1, -1, -1, -1, -1, 1, 1], dtype=np.float32)
        matched = dsp.biphase_matched_filter(shaped.astype(np.complex64), sps_bit=4)
        sampled = matched[0::4]
        np.testing.assert_array_equal(sampled, [-4, 4])
        np.testing.assert_array_equal(dsp.bits_from_symbols_diff(sampled), [1])


class TestSymbolOffset:
    def test_picks_strongest_phase(self):
        # 16 samples per symbol, actual symbols at offset 7
        rng = np.random.default_rng(42)
        sps = 16
        n_symbols = 50
        symbols = rng.choice([-1.0, 1.0], size=n_symbols).astype(np.complex64)
        oversampled = np.zeros(n_symbols * sps, dtype=np.complex64)
        oversampled[7::sps] = symbols
        # Add a noise floor (seeded for reproducibility)
        noise = (
            rng.standard_normal(len(oversampled)) + 1j * rng.standard_normal(len(oversampled))
        ) * 0.05
        oversampled = oversampled + noise.astype(np.complex64)
        out, offset = dsp.best_symbol_offset(oversampled, sps)
        assert offset == 7


class TestClockRecovery:
    def test_mm_updates_after_three_recovered_symbols(self):
        recovered = dsp.clock_recovery_mm(np.ones(80, dtype=np.complex64), sps=1.0)
        assert len(recovered) >= 3


class TestBitsFromSymbols:
    def test_diff_decoding(self):
        # Symbols representing tx_bits [0,1,0,0,1] -> data_bits[n] = tx[n] XOR tx[n-1]
        tx = np.array([0, 1, 0, 0, 1], dtype=np.uint8)
        symbols = (tx.astype(np.float32) * 2 - 1).astype(np.complex64)
        data = dsp.bits_from_symbols_diff(symbols)
        # tx[n]^tx[n-1]: (1^0)=1, (0^1)=1, (0^0)=0, (1^0)=1
        np.testing.assert_array_equal(data, [1, 1, 0, 1])
