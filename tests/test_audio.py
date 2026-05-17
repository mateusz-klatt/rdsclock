"""Tests for the audio module.

The numerical pieces (`fm_demod`, `design_audio_filter`, `fm_audio_from_iq`)
are pure-numpy and tested directly. `play_iq_live` and `play_iq_file` open
an audio device and are not exercised here — they are integration-only.
"""

import numpy as np
import pytest

from rdsclock.audio import (
    DEFAULT_AUDIO_RATE,
    DEFAULT_LIVE_FS,
    design_audio_filter,
    fm_audio_from_iq,
    fm_demod,
)


class TestFmDemod:
    def test_constant_phase_yields_zero(self):
        iq = np.ones(256, dtype=np.complex64) * 0.5
        out = fm_demod(iq)
        assert len(out) == 255
        assert np.allclose(out, 0.0, atol=1e-5)

    def test_constant_frequency_yields_constant_phase_difference(self):
        fs = 250_000
        f0 = 10_000
        n = np.arange(2000)
        iq = np.exp(1j * 2 * np.pi * f0 * n / fs).astype(np.complex64)
        out = fm_demod(iq)
        expected = 2 * np.pi * f0 / fs
        assert np.allclose(out[10:-10], expected, atol=1e-4)


class TestDesignAudioFilter:
    def test_returns_integer_decimation(self):
        taps, decim = design_audio_filter(1_200_000, 48_000)
        assert decim == 25
        assert taps.dtype == np.float32
        assert len(taps) == 129  # default numtaps

    def test_rejects_non_integer_ratio(self):
        with pytest.raises(ValueError):
            design_audio_filter(1_000_000, 48_000)


class TestFmAudioFromIq:
    def test_output_rate_and_normalisation(self):
        fs_in = 1_200_000
        fs_out = 48_000
        n = np.arange(fs_in // 2)  # 0.5 s of input
        # Build an IQ stream FM-modulated by a 3 kHz sine — that is a valid audio tone.
        f_audio = 3_000
        deviation = 50_000
        t = n / fs_in
        phase = 2 * np.pi * np.cumsum(deviation * np.sin(2 * np.pi * f_audio * t)) / fs_in
        iq = np.exp(1j * phase).astype(np.complex64)

        audio = fm_audio_from_iq(iq, fs_in=fs_in, fs_out=fs_out)
        expected_len = (fs_in // 2) // 25  # input length / decimation
        # The FIR adds n-1 samples that fm_demod drops, so allow a small slack.
        assert abs(len(audio) - expected_len) <= 1
        # Normalisation pushes peak to 0.5.
        assert np.max(np.abs(audio)) == pytest.approx(0.5, rel=0.01)

    def test_skip_normalisation(self):
        fs_in = 1_200_000
        n = np.arange(fs_in // 2)
        iq = np.exp(1j * 2 * np.pi * 1_000 * n / fs_in).astype(np.complex64)
        audio_norm = fm_audio_from_iq(iq, fs_in=fs_in, normalise=True)
        audio_raw = fm_audio_from_iq(iq, fs_in=fs_in, normalise=False)
        assert np.max(np.abs(audio_norm)) == pytest.approx(0.5, rel=0.01)
        assert np.max(np.abs(audio_raw)) != pytest.approx(0.5, rel=0.01)


def test_default_constants_make_an_integer_decim():
    """The module-level defaults must be compatible with design_audio_filter."""
    assert DEFAULT_LIVE_FS % DEFAULT_AUDIO_RATE == 0
