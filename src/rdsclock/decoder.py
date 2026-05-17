"""End-to-end pipeline: IQ → bits → groups → ``StationInfo`` (incl. clock).

Side-effect-free interface that returns data. For diagnostics, every
function accepts an optional ``progress`` callback.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from . import dsp
from .rds_blocks import find_groups_in_bitstream
from .rds_clock import ClockTime
from .rds_groups import StationInfo, parse_groups


@dataclass
class DecodeResult:
    """Result of decoding a single IQ stream."""

    info: StationInfo
    n_groups: int
    n_bits: int
    freq_offset_hz: float
    symbol_offset: int

    @property
    def clock_times(self) -> list[ClockTime]:
        return self.info.clock_times


def decode_iq(
    iq: np.ndarray,
    fs: float = dsp.DEFAULT_INPUT_FS,
    carrier_hz: float | None = None,
    symbol_lpf_hz: float = dsp.SYMBOL_LPF_HZ,
    costas_alpha: float = 0.3,
    costas_beta: float = 0.005,
    auto_carrier: bool = True,
    progress: Callable[[str], None] | None = None,
) -> DecodeResult:
    """Run the full pipeline. Returns a ``DecodeResult`` aggregating groups.

    If ``auto_carrier`` is True and ``carrier_hz`` is ``None``, the RDS
    subcarrier is located via the 19 kHz pilot harmonic (tolerates a
    drift of ±6 kHz typical for cheap RTL-SDR dongles).
    """

    def emit(msg: str) -> None:
        if progress:
            progress(msg)

    emit(f"channel_filter (cutoff {dsp.FM_CHANNEL_BW_HZ} Hz)")
    iq = dsp.channel_filter(iq, fs)

    emit("fm_demod")
    fm = dsp.fm_demod(iq)

    if carrier_hz is None:
        if auto_carrier:
            carrier_hz = dsp.estimate_rds_carrier(fm, fs)
            emit(
                f"auto carrier: {carrier_hz:.1f} Hz (drift {carrier_hz - dsp.RDS_CARRIER_HZ:+.1f})"
            )
        else:
            carrier_hz = dsp.RDS_CARRIER_HZ

    emit(f"shift_and_filter @ {carrier_hz:.1f} Hz")
    rds_complex = dsp.shift_and_filter(fm, fs, carrier=carrier_hz)

    emit("decimate to 19 kS/s")
    rds_bb = dsp.decimate_to_rds_rate(rds_complex, input_fs=fs)

    emit("coarse_freq_correction")
    rds_bb, freq_off = dsp.coarse_freq_correction(rds_bb, dsp.DEFAULT_RDS_FS)

    emit(f"symbol_lpf (cutoff {symbol_lpf_hz} Hz)")
    rds_filtered = dsp.symbol_lpf(rds_bb, dsp.DEFAULT_RDS_FS, cutoff=symbol_lpf_hz)

    emit("agc")
    rds_agc = dsp.agc(rds_filtered)

    emit("costas_loop_bpsk")
    rds_sync = dsp.costas_loop_bpsk(rds_agc, alpha=costas_alpha, beta=costas_beta)

    sps = int(round(dsp.DEFAULT_RDS_FS / dsp.RDS_SYMBOL_RATE))
    emit(f"biphase matched filter + offset search (sps {sps})")
    matched = dsp.biphase_matched_filter(rds_sync, sps_bit=sps)
    best_groups: list[bytearray] | None = None
    best_bits: np.ndarray | None = None
    variant_used = "biphase/none"
    sym_offset = 0
    for offset in range(sps):
        sampled = matched[offset::sps]
        if len(sampled) < 100:
            continue
        bits_candidate = dsp.bits_from_symbols_diff(sampled)
        groups_candidate, variant_candidate = _best_variant_groups(bits_candidate)
        if best_groups is None or len(groups_candidate) > len(best_groups):
            best_groups = groups_candidate
            best_bits = bits_candidate
            variant_used = f"biphase/off{offset}/{variant_candidate}"
            sym_offset = offset

    if best_groups is None or best_bits is None:
        # Very short streams may not have enough biphase samples for the
        # offset sweep. Preserve the legacy paths for those callers.
        emit(f"clock recovery fallback: best_offset + mueller_muller (sps {sps})")
        bo_symbols, bo_sym_off = dsp.best_symbol_offset(rds_sync, sps=sps)
        bo_bits = dsp.bits_from_symbols_diff(bo_symbols)
        bo_groups, bo_variant = _best_variant_groups(bo_bits)

        mm_symbols = dsp.clock_recovery_mm(rds_sync, sps=sps)
        mm_bits = dsp.bits_from_symbols_diff(mm_symbols)
        mm_groups, mm_variant = _best_variant_groups(mm_bits)

        if len(mm_groups) > len(bo_groups):
            groups = mm_groups
            variant_used = f"mm/{mm_variant}"
            bits = mm_bits
            sym_offset = -1  # MM has no fixed offset, only its mu state
        else:
            groups = bo_groups
            variant_used = f"bo/{bo_variant}"
            bits = bo_bits
            sym_offset = bo_sym_off
    else:
        groups = best_groups
        bits = best_bits

    info = parse_groups(groups)
    emit(f"groups={len(groups)} variant={variant_used} freq_off={freq_off:+.1f} Hz")

    return DecodeResult(
        info=info,
        n_groups=len(groups),
        n_bits=len(bits),
        freq_offset_hz=float(freq_off),
        symbol_offset=sym_offset,
    )


def _best_variant_groups(bits: np.ndarray) -> tuple[list[bytearray], str]:
    """Try the four bitstream polarity/order variants and return the one
    that yields the most groups.

    Costas may lock 180° out of phase; differential decoding usually
    handles that, but trying the inverted polarity is a cheap safety net.
    The reversed variants catch rare edge cases where the pipeline
    flips symbol order (mainly relevant for short streams).
    """
    candidates = [
        ("normal", bits),
        ("inverted", 1 - bits),
        ("reversed", bits[::-1]),
        ("inv+rev", (1 - bits)[::-1]),
    ]
    best: list[bytearray] = []
    best_name = "normal"
    for name, bstream in candidates:
        g = find_groups_in_bitstream(np.ascontiguousarray(bstream))
        if len(g) > len(best):
            best = g
            best_name = name
    return best, best_name


def decode_file(
    path: str,
    fs: float = dsp.DEFAULT_INPUT_FS,
    progress: Callable[[str], None] | None = None,
    fmt: str | None = None,
) -> DecodeResult:
    """Load an IQ file and decode it.

    ``fmt`` is either ``"u8"`` (rtl_sdr interleaved uint8) or ``"complex64"``.
    When ``None``, the format is autodetected: complex64 files are sized as
    a multiple of 8 bytes and have ``|sample|`` of order unity; uint8 files
    have every byte in [0, 255]. A short prefix is sniffed to disambiguate.
    """
    size = os.path.getsize(path)
    if fmt is None:
        if size % 8 == 0:
            sample = np.fromfile(path, dtype=np.complex64, count=256)
            if (
                len(sample) >= 64
                and np.all(np.isfinite(sample))
                and np.max(np.abs(sample)) < 100.0
                and np.max(np.abs(sample)) > 1e-6
            ):
                fmt = "complex64"
            else:
                fmt = "u8"
        else:
            fmt = "u8"

    if fmt == "complex64":
        iq = dsp.read_iq_complex64(path)
    elif fmt == "u8":
        iq = dsp.read_iq_u8(path)
    else:
        raise ValueError(f"unknown IQ format: {fmt!r}")

    if len(iq) == 0:
        raise ValueError(f"empty IQ file: {path}")
    return decode_iq(iq, fs=fs, progress=progress)
