"""DSP primitives for the RDS decoder.

All functions are deterministic and side-effect-free. They operate on
NumPy ndarrays (typically ``complex64`` for IQ samples and ``float32``
for FM-demodulated baseband).
"""

import numpy as np
from scipy.signal import filtfilt, firwin, lfilter, resample_poly

try:
    from numba import njit as _numba_njit
except ImportError:  # pragma: no cover - optional acceleration dependency
    _numba_njit = None

# RDS / FM constants
FM_CHANNEL_BW_HZ = 100_000  # half-width of an FM channel (~200 kHz total)
RDS_CARRIER_HZ = 57_000
RDS_SYMBOL_RATE = 1187.5  # bits per second
RDS_BANDPASS_HALF_HZ = 6_000  # ±6 kHz around the subcarrier
RDS_BB_LPF_HZ = 4_000  # LPF after shifting to DC
SYMBOL_LPF_HZ = 4_000  # LPF in the symbol-rate domain (after decimation)

DEFAULT_INPUT_FS = 250_000  # rtl_sdr -s 250000
DEFAULT_RDS_FS = 19_000  # after decimation; ~16 samples/symbol


def read_iq_u8(path: str) -> np.ndarray:
    """Load IQ samples in rtl_sdr format (interleaved uint8 with bias 127.5)."""
    raw = np.fromfile(path, dtype=np.uint8)
    if raw.size % 2:
        raw = raw[:-1]
    f = (raw.astype(np.float32) - 127.5) / 127.5
    return (f[0::2] + 1j * f[1::2]).astype(np.complex64)


def read_iq_complex64(path: str) -> np.ndarray:
    """Load IQ samples already stored as little-endian ``complex64``."""
    return np.fromfile(path, dtype=np.complex64)


def write_iq_u8(samples: np.ndarray, path: str) -> int:
    """Write IQ samples in rtl_sdr format (interleaved uint8). Returns count."""
    samples = np.asarray(samples)
    i = np.clip(np.real(samples) * 127.5 + 127.5, 0, 255).astype(np.uint8)
    q = np.clip(np.imag(samples) * 127.5 + 127.5, 0, 255).astype(np.uint8)
    out = np.empty(len(samples) * 2, dtype=np.uint8)
    out[0::2] = i
    out[1::2] = q
    out.tofile(path)
    return len(samples)


def write_iq_complex64(samples: np.ndarray, path: str) -> int:
    samples = np.asarray(samples, dtype=np.complex64)
    samples.tofile(path)
    return len(samples)


def channel_filter(iq: np.ndarray, fs: float, cutoff: float = FM_CHANNEL_BW_HZ) -> np.ndarray:
    """Lowpass FIR cutting out a single FM channel (~±100 kHz)."""
    taps = firwin(numtaps=129, cutoff=cutoff, fs=fs)
    return lfilter(taps, 1.0, iq)


def fm_demod(iq: np.ndarray) -> np.ndarray:
    """FM demodulation by instantaneous-phase differentiation:
    ``arg(z[n] * conj(z[n-1]))``."""
    z = iq[1:] * np.conj(iq[:-1])
    return np.angle(z).astype(np.float32)


def shift_and_filter(
    baseband: np.ndarray, fs: float, carrier: float = RDS_CARRIER_HZ
) -> np.ndarray:
    """Bandpass the signal around the RDS subcarrier, mix to DC, lowpass to RDS BW."""
    bpf = firwin(
        numtaps=321,
        cutoff=[carrier - RDS_BANDPASS_HALF_HZ, carrier + RDS_BANDPASS_HALF_HZ],
        pass_zero=False,
        fs=fs,
    )
    rds_band = lfilter(bpf, 1.0, baseband)
    n = np.arange(len(rds_band))
    mixed = rds_band * np.exp(-1j * 2 * np.pi * carrier * n / fs)
    lpf = firwin(numtaps=161, cutoff=RDS_BB_LPF_HZ, fs=fs)
    return lfilter(lpf, 1.0, mixed)


def estimate_pilot_19khz(baseband: np.ndarray, fs: float, search_span_hz: float = 500.0) -> float:
    """Locate the actual 19 kHz stereo pilot frequency.

    The pilot is an unmodulated continuous tone with very high SNR — a
    much better reference than searching for an FFT peak in the
    suppressed-carrier BPSK RDS band.
    """
    seg_len = min(len(baseband), int(fs * 1.0))
    if seg_len < 1024:
        return 19_000.0
    seg = baseband[:seg_len]
    window = np.hanning(len(seg))
    spec = np.abs(np.fft.rfft(seg * window))
    freqs = np.fft.rfftfreq(len(seg), 1.0 / fs)
    mask = (freqs > 19_000.0 - search_span_hz) & (freqs < 19_000.0 + search_span_hz)
    if not np.any(mask):
        return 19_000.0
    sub_spec = spec[mask]
    sub_freqs = freqs[mask]
    return float(sub_freqs[int(np.argmax(sub_spec))])


def estimate_rds_carrier(
    baseband: np.ndarray,
    fs: float,
    around: float = RDS_CARRIER_HZ,
    span: float = RDS_BANDPASS_HALF_HZ,
    use_pilot: bool = True,
) -> float:
    """Estimate the RDS subcarrier frequency.

    By default (``use_pilot=True``) we locate the 19 kHz pilot and
    multiply by 3 — this is the definitional MPX relationship (RDS sits
    on the third harmonic of the pilot). When ``use_pilot=False`` we
    fall back to a raw FFT peak in the RDS band, which is less reliable
    because BPSK has a suppressed carrier.
    """
    if use_pilot:
        pilot = estimate_pilot_19khz(baseband, fs)
        candidate = pilot * 3.0
        # Sanity: if the pilot estimate is poor, the candidate may be
        # outside the expected RDS band. Fall through to FFT in that case.
        if abs(candidate - around) <= span:
            return candidate
    seg_len = min(len(baseband), int(fs * 1.0))
    if seg_len < 1024:
        return float(around)
    seg = baseband[:seg_len]
    window = np.hanning(len(seg))
    spec = np.abs(np.fft.rfft(seg * window))
    freqs = np.fft.rfftfreq(len(seg), 1.0 / fs)
    mask = (freqs > around - span) & (freqs < around + span)
    if not np.any(mask):
        return float(around)
    sub_spec = spec[mask]
    sub_freqs = freqs[mask]
    return float(sub_freqs[int(np.argmax(sub_spec))])


def coarse_freq_correction(x: np.ndarray, fs: float) -> tuple[np.ndarray, float]:
    """Remove residual frequency offset using a phase-difference estimator."""
    if len(x) < 2:
        return x, 0.0
    ph = np.angle(np.sum(x[1:] * np.conj(x[:-1])))
    freq_est = ph * fs / (2 * np.pi)
    n = np.arange(len(x))
    return x * np.exp(-1j * 2 * np.pi * freq_est * n / fs), float(freq_est)


def symbol_lpf(x: np.ndarray, fs: float, cutoff: float = SYMBOL_LPF_HZ) -> np.ndarray:
    """Zero-phase LPF at the 19 kHz symbol domain prior to clock recovery.

    ``filtfilt`` requires ``len(x) > 3 * (len(taps) - 1)``. For short
    streams we fall back to ``lfilter`` (at the cost of phase).
    """
    n_taps = 81
    if len(x) < 3 * (n_taps - 1):
        taps = firwin(numtaps=n_taps, cutoff=cutoff, fs=fs)
        return lfilter(taps, 1.0, x)
    taps = firwin(numtaps=n_taps, cutoff=cutoff, fs=fs)
    return filtfilt(taps, [1.0], x)


def _costas_loop_bpsk_python(samples: np.ndarray, alpha: float, beta: float) -> np.ndarray:
    out = np.empty_like(samples)
    phase = 0.0
    freq = 0.0
    for i, s in enumerate(samples):
        rotated = s * np.exp(-1j * phase)
        out[i] = rotated
        err = float(np.real(rotated) * np.imag(rotated))
        freq += beta * err
        phase += freq + alpha * err
        if phase > np.pi:
            phase -= 2 * np.pi
        elif phase < -np.pi:
            phase += 2 * np.pi
    return out


_COSTAS_LOOP_BPSK_NUMBA = (
    _numba_njit(cache=True)(_costas_loop_bpsk_python) if _numba_njit is not None else None
)


def costas_loop_bpsk(samples: np.ndarray, alpha: float = 0.3, beta: float = 0.005) -> np.ndarray:
    """Second-order Costas loop for BPSK. Stabilises carrier phase."""
    samples = np.asarray(samples, dtype=np.complex64)
    if _COSTAS_LOOP_BPSK_NUMBA is not None:
        return _COSTAS_LOOP_BPSK_NUMBA(samples, alpha, beta)  # pragma: no cover
    return _costas_loop_bpsk_python(samples, alpha, beta)


def agc(samples: np.ndarray, eps: float = 1e-9) -> np.ndarray:
    """Normalize complex baseband amplitude by its mean magnitude."""
    samples = np.asarray(samples, dtype=np.complex64)
    return (samples / (np.abs(samples).mean() + eps)).astype(np.complex64)


def biphase_matched_filter(post_costas_samples: np.ndarray, sps_bit: int = 16) -> np.ndarray:
    """Matched-filter Manchester/biphase symbols without choosing a clock offset."""
    sps_half = sps_bit // 2
    template = np.concatenate(
        [np.ones(sps_half, dtype=np.float32), -np.ones(sps_half, dtype=np.float32)]
    )
    return np.convolve(np.real(post_costas_samples), template, mode="valid")


def best_symbol_offset(samples: np.ndarray, sps: int) -> tuple[np.ndarray, int]:
    """Pick the offset 0..sps-1 with the highest mean magnitude per symbol."""
    energies = [float(np.mean(np.abs(samples[o::sps]))) for o in range(sps)]
    offset = int(np.argmax(energies))
    return samples[offset::sps], offset


def clock_recovery_mm(
    samples: np.ndarray, sps: float, gain_mu: float = 0.03, gain_omega: float = 0.0001
) -> np.ndarray:
    """Mueller & Müller timing recovery (BPSK, hard-decision feedback).

    Input is oversampled (~``sps`` samples per symbol); output is one
    sample per symbol with a corrected sampling phase.
    """
    if len(samples) < int(sps) * 4:
        return np.zeros(0, dtype=np.complex64)
    interp = resample_poly(samples, 32, 1)
    omega = float(sps)
    mu = 0.0
    out = []
    rail = []
    i_in = 0
    while i_in + 32 < len(samples):
        idx = int(i_in * 32 + mu * 32)
        if idx >= len(interp):
            break
        s = interp[idx]
        out.append(s)
        rail.append(np.sign(np.real(s)) + 1j * np.sign(np.imag(s)))
        if len(out) >= 3:
            x = (rail[-1] - rail[-3]) * np.conj(out[-2])
            y = (out[-1] - out[-3]) * np.conj(rail[-2])
            mm_val = float(np.real(y - x))
        else:
            mm_val = 0.0
        omega += gain_omega * mm_val
        mu += omega + gain_mu * mm_val
        step = int(np.floor(mu))
        i_in += step
        mu -= step
    return np.array(out, dtype=np.complex64)


def bits_from_symbols_diff(symbols: np.ndarray) -> np.ndarray:
    """Hard-decide sampled biphase symbols and differentially decode.

    The input is the biphase matched-filter output sampled once per bit.
    Hard decisions recover the transmitted differential bitstream, and
    XOR'ing adjacent decisions recovers the original data bits.
    """
    hard = (np.real(symbols) >= 0).astype(np.uint8)
    if len(hard) < 2:
        return np.zeros(0, dtype=np.uint8)
    return np.bitwise_xor(hard[1:], hard[:-1])


def decimate_to_rds_rate(rds_complex: np.ndarray, input_fs: float = DEFAULT_INPUT_FS) -> np.ndarray:
    """Decimate baseband from ~250 kS/s down to 19 kS/s (16 samples/symbol).

    For the standard 250 kS/s path: drop every 5th sample (250 → 50 kS/s)
    then ``resample_poly(19, 50)`` → 19 kS/s. For other input rates we use
    a generic polyphase resampler to 19 kHz.
    """
    if input_fs != DEFAULT_INPUT_FS:
        return resample_poly(rds_complex, DEFAULT_RDS_FS, int(round(input_fs)))
    return resample_poly(rds_complex[::5], 19, 50)
