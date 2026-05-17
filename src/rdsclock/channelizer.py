"""Digital channelizer: wide-band IQ → N narrow per-station channels.

The classic way to decode several FM stations with a single RTL-SDR:
capture (for example) 2.4 MS/s centred between the stations of interest,
then for each station shift it to baseband and decimate to 250 kS/s.
"""

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from math import gcd

import numpy as np
from scipy.signal import firwin, lfilter, resample_poly

from . import dsp
from .decoder import DecodeResult, decode_iq


@dataclass(frozen=True)
class ChannelSpec:
    """Specification of one channel to extract from a wide-band capture."""

    freq_hz: float  # absolute station frequency, e.g. 95.5e6
    label: str = ""  # human-friendly name, e.g. "RMF FM 95.5"


def extract_channel(
    iq_wide: np.ndarray,
    fs_wide: float,
    f_center: float,
    f_channel: float,
    fs_out: float = dsp.DEFAULT_INPUT_FS,
    half_bw: float = 100_000.0,
) -> np.ndarray:
    """Extract a single FM channel (typically 250 kS/s) from a wide capture.

    1. Frequency shift the channel to DC.
    2. Lowpass to ``half_bw`` (≈100 kHz for FM).
    3. Decimate to ``fs_out``.
    """
    if fs_out > fs_wide:
        raise ValueError("fs_out must be <= fs_wide")
    delta = f_channel - f_center
    n = np.arange(len(iq_wide), dtype=np.float64)
    mixed = (iq_wide * np.exp(-1j * 2 * np.pi * delta * n / fs_wide)).astype(np.complex64)

    # Lowpass designed at fs_wide with cutoff = half_bw
    n_taps = 257
    taps = firwin(numtaps=n_taps, cutoff=half_bw, fs=fs_wide).astype(np.float32)
    filtered = lfilter(taps, 1.0, mixed)

    a = int(round(fs_out))
    b = int(round(fs_wide))
    g = gcd(a, b)
    up, down = a // g, b // g
    return resample_poly(filtered, up, down).astype(np.complex64)


@dataclass
class ChannelDecodeResult:
    spec: ChannelSpec
    iq_samples: int
    result: DecodeResult


def decode_channels(
    iq_wide: np.ndarray,
    fs_wide: float,
    f_center: float,
    channels: Sequence[ChannelSpec],
    fs_out: float = dsp.DEFAULT_INPUT_FS,
    max_workers: int = 4,
    progress: Callable[[str], None] | None = None,
) -> list[ChannelDecodeResult]:
    """For each channel: extract → decode. Parallel via ``ThreadPoolExecutor``."""

    def emit(msg: str) -> None:
        if progress:
            progress(msg)

    def worker(spec: ChannelSpec) -> ChannelDecodeResult:
        emit(f"channel {spec.label or f'{spec.freq_hz / 1e6:.2f}MHz'}: extract")
        iq_chan = extract_channel(iq_wide, fs_wide, f_center, spec.freq_hz, fs_out=fs_out)
        emit(f"channel {spec.label or f'{spec.freq_hz / 1e6:.2f}MHz'}: decode")
        res = decode_iq(iq_chan, fs=fs_out)
        return ChannelDecodeResult(spec=spec, iq_samples=len(iq_chan), result=res)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(worker, channels))
    return results


def auto_center(freqs_hz: Sequence[float]) -> float:
    """Mid-point of a set of station frequencies (used to centre the SDR)."""
    if not freqs_hz:
        raise ValueError("freqs_hz must be non-empty")
    return (max(freqs_hz) + min(freqs_hz)) / 2.0


def required_bandwidth(freqs_hz: Sequence[float], guard_hz: float = 200_000.0) -> float:
    """Minimum sample rate needed to capture all stations with a guard band."""
    if not freqs_hz:
        raise ValueError("freqs_hz must be non-empty")
    span = max(freqs_hz) - min(freqs_hz)
    return span + 2 * guard_hz


def fits_in_window(freqs_hz: Sequence[float], fs_wide: float, guard_hz: float = 200_000.0) -> bool:
    """Whether all stations fit inside a single capture at ``fs_wide``."""
    return required_bandwidth(freqs_hz, guard_hz=guard_hz) <= fs_wide
