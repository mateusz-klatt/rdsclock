"""Tests for the plot module — both the pure-numpy core and the
matplotlib-backed renderers (running headless via the Agg backend)."""

import numpy as np
import pytest

# Matplotlib is an optional dependency; skip the whole module when it is missing.
matplotlib = pytest.importorskip("matplotlib")
matplotlib.use("Agg")  # headless backend before importing pyplot indirectly

from rdsclock.plot import (  # noqa: E402  (must follow Agg backend setup)
    mpx_spectrum,
    plot_iq_waterfall,
    plot_mpx_spectrum,
)


def _synthetic_iq(duration_s: float = 0.5, fs: int = 250_000) -> np.ndarray:
    """Build a minimal FM-modulated IQ stream with a clear 19 kHz pilot
    so the renderers have something concrete to find."""
    from rdsclock.rds_groups import encode_ps_groups
    from rdsclock.synth import synthesize_fm_iq

    groups = encode_ps_groups(pi=0xCAFE, ps_name="TESTPLOT")
    return synthesize_fm_iq(groups, duration_s=duration_s, fs=fs, snr_db=None)


class TestMpxSpectrumCore:
    def test_returns_freq_and_magnitude_arrays(self):
        iq = _synthetic_iq()
        freqs, mag = mpx_spectrum(iq, fs=250_000)
        assert len(freqs) == len(mag)
        assert freqs[0] == 0.0
        assert freqs[-1] == pytest.approx(125_000.0, rel=1e-3)  # Nyquist for 250 kS/s
        assert mag.dtype == np.float32

    def test_rejects_short_input(self):
        iq = np.ones(512, dtype=np.complex64)
        with pytest.raises(ValueError):
            mpx_spectrum(iq, fs=250_000)

    def test_pilot_peak_present(self):
        iq = _synthetic_iq()
        freqs, mag = mpx_spectrum(iq, fs=250_000)
        # The 19 kHz pilot should be the strongest tone in the 15..23 kHz window
        mask_pilot = (freqs > 18_000) & (freqs < 20_000)
        mask_else = (freqs > 1_000) & (freqs < 15_000)
        assert mag[mask_pilot].max() > mag[mask_else].max()

    def test_smaller_fft_size_still_runs(self):
        iq = _synthetic_iq()
        freqs, mag = mpx_spectrum(iq, fs=250_000, fft_size=4096)
        assert len(freqs) == 2049  # 4096/2 + 1


class TestPlotMpxSpectrum:
    def test_writes_png_to_explicit_path(self, tmp_path):
        iq = _synthetic_iq()
        out = tmp_path / "mpx.png"
        result = plot_mpx_spectrum(iq, fs=250_000, out_path=str(out), title="t")
        assert result == str(out)
        assert out.exists()
        # Non-trivial size: matplotlib writes at least a few kB.
        assert out.stat().st_size > 5_000

    def test_default_output_path(self, tmp_path, monkeypatch):
        # Run inside tmp_path so the default mpx_spectrum.png lands there.
        monkeypatch.chdir(tmp_path)
        iq = _synthetic_iq()
        out = plot_mpx_spectrum(iq, fs=250_000)
        assert out == "mpx_spectrum.png"
        assert (tmp_path / "mpx_spectrum.png").exists()


class TestPlotIqWaterfall:
    def test_writes_png_to_explicit_path(self, tmp_path):
        iq = _synthetic_iq(duration_s=0.3)
        out = tmp_path / "wf.png"
        result = plot_iq_waterfall(iq, fs=250_000, out_path=str(out), nperseg=2048)
        assert result == str(out)
        assert out.exists()
        assert out.stat().st_size > 5_000

    def test_default_output_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        iq = _synthetic_iq(duration_s=0.3)
        out = plot_iq_waterfall(iq, fs=250_000, nperseg=2048)
        assert out == "iq_waterfall.png"
        assert (tmp_path / "iq_waterfall.png").exists()
