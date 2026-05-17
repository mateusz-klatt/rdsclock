"""Spectrum plots for IQ captures.

Renders an annotated FFT view of an FM MPX baseband (after FM
demodulation): mono audio (≤15 kHz), 19 kHz pilot, 38 kHz stereo
subcarrier and 57 kHz RDS subcarrier. Useful for:

- proving that a real-world capture contains a stereo + RDS station,
- visualising the RDS subcarrier drift due to tuner ppm,
- presentation-quality figures for documentation.

``matplotlib`` is an optional dependency (extra ``[plot]``).
"""

import numpy as np

from . import dsp

DEFAULT_FFT_SIZE = 1 << 16  # 65536 samples — ~262 ms at 250 kS/s, ~55 ms at 1.2 MS/s


def mpx_spectrum(
    iq: np.ndarray,
    fs: float,
    fft_size: int = DEFAULT_FFT_SIZE,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (freq_hz, magnitude_db) of the FM-demodulated baseband spectrum."""
    iq = dsp.channel_filter(iq, fs)
    fm = dsp.fm_demod(iq)
    seg_len = min(len(fm), fft_size)
    if seg_len < 1024:
        raise ValueError(f"need at least 1024 samples after FM demod, have {seg_len}")
    seg = fm[:seg_len]
    window = np.hanning(len(seg))
    spec = np.fft.rfft(seg * window)
    mag = 20 * np.log10(np.abs(spec) + 1e-12)
    freqs = np.fft.rfftfreq(len(seg), 1.0 / fs)
    return freqs, mag.astype(np.float32)


def plot_mpx_spectrum(
    iq: np.ndarray,
    fs: float,
    title: str = "FM MPX spectrum",
    out_path: str | None = None,
    fft_size: int = DEFAULT_FFT_SIZE,
    show: bool = False,
) -> str:
    """Render the FM MPX spectrum with annotated peaks (mono/pilot/stereo/RDS).

    Saves to ``out_path`` (PNG) if provided; otherwise generates one in
    the current directory. Returns the path to the saved figure.
    """
    try:
        import matplotlib
    except ImportError as exc:
        raise RuntimeError(
            "plotting requires the optional 'matplotlib' dependency; "
            "install with: pip install 'rdsclock[plot]'"
        ) from exc
    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    freqs, mag = mpx_spectrum(iq, fs, fft_size=fft_size)

    # Limit to 0..70 kHz where the interesting MPX components live.
    mask = freqs <= 70_000.0
    freqs_khz = freqs[mask] / 1000.0
    mag_db = mag[mask]

    fig, ax = plt.subplots(figsize=(10, 5), constrained_layout=True)
    ax.plot(freqs_khz, mag_db, color="#1f77b4", linewidth=0.8)
    ax.set_xlabel("Frequency [kHz]")
    ax.set_ylabel("Magnitude [dB]")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)

    # Annotated bands
    annotations = [
        (0, 15, "Mono audio (L+R)", "#2ca02c"),
        (18.5, 19.5, "19 kHz pilot", "#d62728"),
        (23, 53, "Stereo (L-R) DSB", "#9467bd"),
        (55, 59, "RDS 57 kHz", "#ff7f0e"),
    ]
    y_min, y_max = ax.get_ylim()
    for x0, x1, label, colour in annotations:
        ax.axvspan(x0, x1, alpha=0.12, color=colour)
        ax.text(
            (x0 + x1) / 2,
            y_max - (y_max - y_min) * 0.06,
            label,
            color=colour,
            ha="center",
            fontsize=9,
            fontweight="bold",
        )

    # Locate the actual pilot for a precise label.
    try:
        pilot_hz = dsp.estimate_pilot_19khz(dsp.fm_demod(dsp.channel_filter(iq, fs)), fs)
        ax.axvline(pilot_hz / 1000.0, color="#d62728", linestyle="--", alpha=0.6)
        ax.text(
            pilot_hz / 1000.0 + 0.3,
            y_min + (y_max - y_min) * 0.08,
            f"pilot @ {pilot_hz:.0f} Hz",
            color="#d62728",
            fontsize=8,
        )
    except Exception:
        pass

    if out_path is None:
        out_path = "mpx_spectrum.png"
    fig.savefig(out_path, dpi=140)
    if show:
        plt.show()
    plt.close(fig)
    return out_path


def plot_iq_waterfall(
    iq: np.ndarray,
    fs: float,
    title: str = "IQ spectrogram",
    out_path: str | None = None,
    nperseg: int = 4096,
    show: bool = False,
) -> str:
    """Render a waterfall (spectrogram) of the raw IQ stream.

    Useful for visualising station spread across a wide capture.
    """
    try:
        import matplotlib
    except ImportError as exc:
        raise RuntimeError(
            "plotting requires the optional 'matplotlib' dependency; "
            "install with: pip install 'rdsclock[plot]'"
        ) from exc
    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy.signal import spectrogram

    f, t, sxx = spectrogram(
        iq, fs=fs, nperseg=nperseg, noverlap=nperseg // 2, return_onesided=False
    )
    # Re-centre so DC is in the middle of the y-axis.
    f = np.fft.fftshift(f)
    sxx = np.fft.fftshift(sxx, axes=0)
    sxx_db = 10 * np.log10(np.abs(sxx) + 1e-12)

    fig, ax = plt.subplots(figsize=(10, 5), constrained_layout=True)
    im = ax.imshow(
        sxx_db,
        aspect="auto",
        origin="lower",
        extent=[t[0], t[-1], f[0] / 1e3, f[-1] / 1e3],
        cmap="viridis",
    )
    ax.set_xlabel("Time [s]")
    ax.set_ylabel("Frequency offset [kHz]")
    ax.set_title(title)
    fig.colorbar(im, ax=ax, label="dB")
    if out_path is None:
        out_path = "iq_waterfall.png"
    fig.savefig(out_path, dpi=140)
    if show:
        plt.show()
    plt.close(fig)
    return out_path
