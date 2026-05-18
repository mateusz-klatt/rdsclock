"""Synthetic FM-RDS signal generator — for unit tests and offline demos.

Pipeline:

    RDS groups (8 bytes each)
       └→ block-level encoding (CRC + offset words)  [rds_blocks]
            └→ raw bitstream
                 └→ differential encoding
                      └→ biphase / Manchester shaping at ~228 kS/s
                           └→ BPSK modulation onto 57 kHz subcarrier
                                └→ MPX baseband (mono audio + 19 kHz pilot + 57 kHz RDS)
                                     └→ FM modulation (integrated MPX as phase)
                                          └→ IQ at ~250 kS/s + optional AWGN
"""

from collections.abc import Iterable, Sequence
from math import gcd

import numpy as np
from scipy.signal import firwin, lfilter, resample_poly

from .rds_blocks import (
    blocks_to_bits,
    differential_encode,
    encode_group,
    group_bytes_to_words,
)

DEFAULT_FS = 250_000  # final IQ sample rate (rtl_sdr -s 250000)
DEFAULT_INTERMEDIATE_FS = 228_000  # 192 × 1187.5 — exact integer samples/symbol
RDS_CARRIER = 57_000.0
RDS_PILOT = 19_000.0
RDS_SYMBOL_RATE = 1187.5
FM_DEVIATION = 75_000.0  # ±75 kHz (mono FM)
# Practical MPX component levels (sum < 1 to avoid over-deviation in tests)
DEFAULT_AUDIO_LEVEL = 0.45
DEFAULT_PILOT_LEVEL = 0.08
DEFAULT_RDS_LEVEL = 0.04


def rds_groups_to_bits(
    groups: Iterable[Sequence[int]],
    version_b_per_group: Sequence[bool] | None = None,
    differential: bool = True,
    repeat: int = 1,
) -> np.ndarray:
    """Turn a list of 8-byte groups into a transmit bitstream.

    Version A (offset C) is used by default. Pass ``version_b_per_group``
    as a list of bools (same length as ``groups``) to switch individual
    groups to Version B (offset C').

    ``repeat`` carousels the same groups before differential encoding.
    Tiling must happen on raw data bits *before* ``differential_encode``;
    otherwise the carousel boundary injects a spurious bit and shifts
    the group boundaries on subsequent iterations.
    """
    groups = list(groups)
    if version_b_per_group is None:
        version_b_per_group = [False] * len(groups)
    elif len(version_b_per_group) != len(groups):
        raise ValueError("version_b_per_group length must match groups length")

    all_blocks: list[int] = []
    for g, vb in zip(groups, version_b_per_group, strict=True):
        words = group_bytes_to_words(g)
        all_blocks.extend(encode_group(words, version_b=vb))
    data_bits = blocks_to_bits(all_blocks)
    if repeat > 1:
        data_bits = np.tile(data_bits, repeat)
    return differential_encode(data_bits) if differential else data_bits


def biphase_symbols(bits: np.ndarray, samples_per_bit: int) -> np.ndarray:
    """Manchester/biphase symbol shaping for RDS.

    Bit 1 is encoded as a positive half-bit followed by a negative
    half-bit; bit 0 uses the opposite polarity. ``samples_per_bit`` must
    be even so each chip spans the same number of samples.
    """
    if samples_per_bit % 2:
        raise ValueError("samples_per_bit must be even for biphase shaping")
    bits = np.asarray(bits, dtype=np.uint8)
    levels = bits.astype(np.float32) * 2.0 - 1.0
    chips = np.column_stack((levels, -levels)).reshape(-1)
    return np.repeat(chips, samples_per_bit // 2)


def rds_baseband(
    bits: np.ndarray,
    fs: float = DEFAULT_INTERMEDIATE_FS,
    symbol_rate: float = RDS_SYMBOL_RATE,
) -> np.ndarray:
    """Convert raw bits into a band-limited biphase stream ready for mixing."""
    sps = fs / symbol_rate
    if not np.isclose(sps, round(sps)):
        raise ValueError(
            f"fs ({fs}) must be an integer multiple of symbol_rate ({symbol_rate}); sps={sps}"
        )
    sps_int = int(round(sps))
    if sps_int % 2:
        raise ValueError(f"fs ({fs}) must yield an even number of samples per bit; sps={sps_int}")
    biphase = biphase_symbols(bits, sps_int)
    # Biphase coding uses half-bit chips, so preserve energy up to the chip rate.
    taps = firwin(numtaps=2 * sps_int + 1, cutoff=2 * symbol_rate, fs=fs)
    return lfilter(taps, 1.0, biphase).astype(np.float32)


def modulate_rds(
    bits: np.ndarray, fs: float = DEFAULT_INTERMEDIATE_FS, carrier: float = RDS_CARRIER
) -> np.ndarray:
    """BPSK-modulate a bitstream onto ``carrier`` (default 57 kHz)."""
    bb = rds_baseband(bits, fs=fs)
    n = np.arange(len(bb))
    return (bb * np.cos(2 * np.pi * carrier * n / fs)).astype(np.float32)


def make_mpx(
    rds_signal: np.ndarray,
    fs: float,
    audio: np.ndarray | None = None,
    audio_level: float = DEFAULT_AUDIO_LEVEL,
    pilot_level: float = DEFAULT_PILOT_LEVEL,
    rds_level: float = DEFAULT_RDS_LEVEL,
) -> np.ndarray:
    """Build the FM-MPX baseband: mono audio (L+R) + 19 kHz pilot + 57 kHz RDS.

    The audio array and RDS signal must have the same length (or ``audio=None``).
    The pilot is phase-synchronous with the subcarrier (3 × 19 = 57 kHz).
    """
    n_total = len(rds_signal)
    n = np.arange(n_total)

    if audio is None:
        # Silence (a 1 kHz tone could be injected here for audio sanity checks)
        audio_signal = np.zeros(n_total, dtype=np.float32)
    else:
        if len(audio) < n_total:
            audio_signal = np.pad(audio, (0, n_total - len(audio)))
        else:
            audio_signal = audio[:n_total]

    pilot = np.cos(2 * np.pi * RDS_PILOT * n / fs).astype(np.float32)
    return (audio_level * audio_signal + pilot_level * pilot + rds_level * rds_signal).astype(
        np.float32
    )


def fm_modulate(
    mpx: np.ndarray,
    fs: float,
    deviation_hz: float = FM_DEVIATION,
    carrier_offset_hz: float = 0.0,
) -> np.ndarray:
    """Classical FM modulation: ``IQ = exp(j · 2π · ∫deviation · m(t))``.

    ``mpx`` must be normalised to [-1, 1] (after all components are summed).
    """
    mpx = np.asarray(mpx, dtype=np.float32)
    peak = float(np.max(np.abs(mpx)))
    if peak > 1.0:
        mpx = mpx / peak
    integral = np.cumsum(mpx) / fs
    phase = 2 * np.pi * deviation_hz * integral
    if carrier_offset_hz:
        phase += 2 * np.pi * carrier_offset_hz * np.arange(len(mpx)) / fs
    return np.exp(1j * phase).astype(np.complex64)


def resample_to(samples: np.ndarray, from_fs: float, to_fs: float) -> np.ndarray:
    """Resample using ``resample_poly`` with the appropriate up/down ratio."""
    a = int(round(to_fs))
    b = int(round(from_fs))
    g = gcd(a, b)
    up, down = a // g, b // g
    return resample_poly(samples, up, down).astype(samples.dtype)


def _nondeterministic_noise_rng() -> np.random.Generator:
    """Return a fresh, unseeded numpy ``Generator`` for noise synthesis.

    Reproducibility is opt-in: callers that need deterministic output
    pass a pre-seeded ``rng`` to :func:`add_awgn` and friends. This
    helper exists so the deliberate "no seed" choice lives in exactly
    one place (instead of being scattered across call sites) and is
    annotated for the static analyser.
    """
    return np.random.default_rng()  # NOSONAR(S6709)


def add_awgn(
    iq: np.ndarray,
    snr_db: float,
    signal_power: float | None = None,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Add complex AWGN to an IQ stream at the requested SNR (in dB)."""
    iq = np.asarray(iq, dtype=np.complex64)
    if signal_power is None:
        signal_power = float(np.mean(np.abs(iq) ** 2))
    if signal_power <= 0:
        return iq.copy()
    snr_lin = 10 ** (snr_db / 10.0)
    noise_power = signal_power / snr_lin
    if rng is None:
        rng = _nondeterministic_noise_rng()
    # Complex noise: real & imag each N(0, noise_power/2)
    sigma = np.sqrt(noise_power / 2.0)
    noise = (rng.standard_normal(len(iq)) + 1j * rng.standard_normal(len(iq))) * sigma
    return (iq + noise.astype(np.complex64)).astype(np.complex64)


def synthesize_fm_iq(
    groups: Iterable[Sequence[int]],
    duration_s: float,
    fs: float = DEFAULT_FS,
    snr_db: float | None = None,
    carrier_offset_hz: float = 0.0,
    rng: np.random.Generator | None = None,
    audio_tone_hz: float | None = None,
    intermediate_fs: float = DEFAULT_INTERMEDIATE_FS,
) -> np.ndarray:
    """High-level helper: from a list of RDS groups and a duration,
    produce an IQ stream at ``fs``.

    The groups are repeated (carouseled) as needed to fill ``duration_s``.
    """
    groups = list(groups)
    if not groups:
        raise ValueError("groups must be non-empty")
    if duration_s <= 0:
        raise ValueError("duration_s must be > 0")

    # Compute how many carousel iterations are needed to span duration_s.
    n_samples_intermediate = int(round(duration_s * intermediate_fs))
    bits_one_run = rds_groups_to_bits(groups, differential=False)
    samples_per_run = int(round(len(bits_one_run) * (intermediate_fs / RDS_SYMBOL_RATE)))
    runs = max(1, int(np.ceil(n_samples_intermediate / samples_per_run)))
    bits = rds_groups_to_bits(groups, differential=True, repeat=runs)

    rds_sig = modulate_rds(bits, fs=intermediate_fs)
    rds_sig = rds_sig[:n_samples_intermediate]

    if audio_tone_hz is not None and audio_tone_hz > 0:
        n = np.arange(n_samples_intermediate)
        audio = np.sin(2 * np.pi * audio_tone_hz * n / intermediate_fs).astype(np.float32)
    else:
        audio = None

    mpx = make_mpx(rds_sig, fs=intermediate_fs, audio=audio)
    iq = fm_modulate(mpx, fs=intermediate_fs, carrier_offset_hz=carrier_offset_hz)

    if intermediate_fs != fs:
        iq = resample_to(iq, intermediate_fs, fs)

    if snr_db is not None:
        iq = add_awgn(iq, snr_db=snr_db, rng=rng)

    return iq.astype(np.complex64)
