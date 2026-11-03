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
from scipy.signal import firwin, oaconvolve, resample_poly

from . import dsp
from .decoder import DecodeResult, decode_iq


@dataclass(frozen=True)
class ChannelSpec:
    """Specification of one channel to extract from a wide-band capture."""

    freq_hz: float  # absolute station frequency, e.g. 95.5e6
    label: str = ""  # human-friendly name, e.g. "RMF FM 95.5"


# Samples mixed at a time: bounds the float64 phase temporaries to a few MB.
_MIX_BLOCK = 1 << 20


def extract_channel(
    iq_wide: np.ndarray,
    fs_wide: float,
    f_center: float,
    f_channel: float,
    fs_out: float = dsp.DEFAULT_INPUT_FS,
    half_bw: float = 100_000.0,
) -> np.ndarray:
    """Extract a single FM channel (typically 250 kS/s) from a wide capture.

    1. Frequency shift the channel to DC, one block at a time.
    2. Lowpass to ``half_bw`` (≈100 kHz for FM) and decimate to ``fs_out`` in
       one polyphase step.

    Besides the result, this needs one complex64 copy of the capture: about
    1.4 GB for 75 s at 2.4 MS/s. The filter is centred, so output sample
    ``k`` belongs to ``k / fs_out`` seconds after the first input sample.
    """
    if fs_out > fs_wide:
        raise ValueError("fs_out must be <= fs_wide")
    a = int(round(fs_out))
    b = int(round(fs_wide))
    g = gcd(a, b)
    up, down = a // g, b // g

    step = -2 * np.pi * (f_channel - f_center) / fs_wide
    ramp = np.exp(1j * step * np.arange(min(_MIX_BLOCK, len(iq_wide))))
    mixed = np.empty(len(iq_wide), dtype=np.complex64)
    for start in range(0, len(iq_wide), _MIX_BLOCK):
        stop = min(start + _MIX_BLOCK, len(iq_wide))
        phasor = (np.exp(1j * step * start) * ramp[: stop - start]).astype(np.complex64)
        np.multiply(iq_wide[start:stop], phasor, out=mixed[start:stop])

    # resample_poly's own filter design, with the cutoff at half_bw unless the
    # output Nyquist frequency is lower. float32 taps keep the result complex64.
    cutoff = min(half_bw, fs_out / 2)
    half_len = max(10 * max(up, down), 128)
    taps = firwin(2 * half_len + 1, cutoff, window=("kaiser", 5.0), fs=fs_wide * up)
    taps = taps.astype(np.float32)
    if up == down == 1:
        # resample_poly would return an unfiltered copy: filter here, centred.
        return oaconvolve(mixed, taps, mode="same")
    return resample_poly(mixed, up, down, window=taps)


@dataclass
class ChannelDecodeResult:
    spec: ChannelSpec
    iq_samples: int
    result: DecodeResult
    rssi_db: float = float("nan")  # power of the extracted channel, dBFS


def decode_channels(
    iq_wide: np.ndarray,
    fs_wide: float,
    f_center: float,
    channels: Sequence[ChannelSpec],
    fs_out: float = dsp.DEFAULT_INPUT_FS,
    max_workers: int = 4,
    progress: Callable[[str], None] | None = None,
    capture_start_monotonic_ns: int | None = None,
) -> list[ChannelDecodeResult]:
    """For each channel: extract → decode. Parallel via ``ThreadPoolExecutor``.

    Each worker holds one complex64 copy of the capture while it extracts;
    ``max_workers=1`` bounds memory for long captures. With
    ``capture_start_monotonic_ns``, Clock-Time receive times refer to the
    first sample of the wide capture, as for a capture at the station itself.
    """

    def emit(msg: str) -> None:
        if progress:
            progress(msg)

    def worker(spec: ChannelSpec) -> ChannelDecodeResult:
        emit(f"channel {spec.label or f'{spec.freq_hz / 1e6:.2f}MHz'}: extract")
        iq_chan = extract_channel(iq_wide, fs_wide, f_center, spec.freq_hz, fs_out=fs_out)
        rssi_db = float(10 * np.log10(float(np.mean(np.abs(iq_chan) ** 2)) + 1e-12))
        emit(f"channel {spec.label or f'{spec.freq_hz / 1e6:.2f}MHz'}: decode")
        res = decode_iq(iq_chan, fs=fs_out, capture_start_monotonic_ns=capture_start_monotonic_ns)
        return ChannelDecodeResult(spec=spec, iq_samples=len(iq_chan), result=res, rssi_db=rssi_db)

    if max_workers == 1:
        # In the calling thread, so that Ctrl+C does not wait for a long decode.
        return [worker(spec) for spec in channels]
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


def outside_capture(
    freqs_hz: Sequence[float], fs_wide: float, f_center: float, guard_hz: float = 200_000.0
) -> list[float]:
    """The stations closer than ``guard_hz`` to the edges of a capture at ``f_center``."""
    reach = fs_wide / 2 - guard_hz
    return [f for f in freqs_hz if abs(f - f_center) > reach]


def choose_center(
    freqs_hz: Sequence[float],
    fs_wide: float,
    guard_hz: float = 200_000.0,
    dc_clearance_hz: float = 150_000.0,
) -> float:
    """A capture centre for stations that fit in ``fs_wide``.

    The receiver adds its DC offset at the centre and, through I/Q imbalance,
    a mirror image of every station reflected about it. Station *i* meets the
    image of station *j* when the centre lies halfway between them, and the
    DC offset when *i* is *j*. Prefer the point nearest the midpoint of the
    stations that is at least ``dc_clearance_hz`` from all these halfway
    points, while each station stays ``guard_hz`` inside the capture. Return
    the midpoint when no such point exists.
    """
    if not fits_in_window(freqs_hz, fs_wide, guard_hz=guard_hz):
        raise ValueError("the stations do not fit in one capture at fs_wide")
    reach = fs_wide / 2 - guard_hz
    low, high = max(freqs_hz) - reach, min(freqs_hz) + reach
    middle = auto_center(freqs_hz)
    halfway = sorted({(a + b) / 2 for a in freqs_hz for b in freqs_hz})

    def clear(center: float) -> bool:
        # 1 Hz absorbs float rounding at the boundary itself.
        return all(abs(point - center) >= dc_clearance_hz - 1.0 for point in halfway)

    edges = [point + side * dc_clearance_hz for point in halfway for side in (-1, 1)]
    options = [c for c in (middle, low, high, *edges) if low <= c <= high and clear(c)]
    return min(options, key=lambda c: abs(c - middle), default=middle)
