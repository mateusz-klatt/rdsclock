"""End-to-end pipeline: IQ → bits → groups → ``StationInfo`` (incl. clock).

Side-effect-free interface that returns data. For diagnostics, every
function accepts an optional ``progress`` callback.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from . import dsp
from .rds_blocks import (
    GROUP_BITS,
    _find_groups_in_bitstream_with_counts_and_positions,
)
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
    n_groups_clean: int = -1
    n_groups_corrected: int = 0
    group_bit_positions: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.n_groups_clean < 0:
            self.n_groups_clean = self.n_groups

    @property
    def clock_times(self) -> list[ClockTime]:
        return self.info.clock_times


@dataclass
class _DecodeAttempt:
    """Result of one bitstream-recovery attempt."""

    groups: list[bytearray]
    positions: list[int]
    n_clean: int
    n_corrected: int
    bits: np.ndarray
    variant_used: str
    sym_offset: int


def _resolve_carrier(
    fm: np.ndarray,
    fs: float,
    carrier_hz: float | None,
    auto_carrier: bool,
    emit: Callable[[str], None],
) -> float:
    if carrier_hz is not None:
        return carrier_hz
    if not auto_carrier:
        return dsp.RDS_CARRIER_HZ
    carrier_hz = dsp.estimate_rds_carrier(fm, fs)
    emit(f"auto carrier: {carrier_hz:.1f} Hz (drift {carrier_hz - dsp.RDS_CARRIER_HZ:+.1f})")
    return carrier_hz


def _decode_with_offset_sweep(matched: np.ndarray, sps: int) -> _DecodeAttempt | None:
    best: _DecodeAttempt | None = None
    for offset in range(sps):
        sampled = matched[offset::sps]
        if len(sampled) < 100:
            continue
        bits_candidate = dsp.bits_from_symbols_diff(sampled)
        groups_candidate, positions_candidate, variant_candidate, n_clean, n_corrected = (
            _best_variant_groups(bits_candidate)
        )
        candidate_score = _group_score(groups_candidate, n_clean, n_corrected)
        if best is not None and candidate_score <= _group_score(
            best.groups, best.n_clean, best.n_corrected
        ):
            continue
        best = _DecodeAttempt(
            groups=groups_candidate,
            positions=positions_candidate,
            n_clean=n_clean,
            n_corrected=n_corrected,
            bits=bits_candidate,
            variant_used=f"biphase/off{offset}/{variant_candidate}",
            sym_offset=offset,
        )
    return best


def _decode_fallback(rds_sync: np.ndarray, sps: int) -> _DecodeAttempt:
    bo_symbols, bo_sym_off = dsp.best_symbol_offset(rds_sync, sps=sps)
    bo_bits = dsp.bits_from_symbols_diff(bo_symbols)
    bo_groups, bo_positions, bo_variant, bo_clean, bo_corrected = _best_variant_groups(bo_bits)

    mm_symbols = dsp.clock_recovery_mm(rds_sync, sps=sps)
    mm_bits = dsp.bits_from_symbols_diff(mm_symbols)
    mm_groups, mm_positions, mm_variant, mm_clean, mm_corrected = _best_variant_groups(mm_bits)

    if _group_score(mm_groups, mm_clean, mm_corrected) > _group_score(
        bo_groups, bo_clean, bo_corrected
    ):
        return _DecodeAttempt(
            groups=mm_groups,
            positions=mm_positions,
            n_clean=mm_clean,
            n_corrected=mm_corrected,
            bits=mm_bits,
            variant_used=f"mm/{mm_variant}",
            sym_offset=-1,  # MM has no fixed offset, only its mu state
        )
    return _DecodeAttempt(
        groups=bo_groups,
        positions=bo_positions,
        n_clean=bo_clean,
        n_corrected=bo_corrected,
        bits=bo_bits,
        variant_used=f"bo/{bo_variant}",
        sym_offset=bo_sym_off,
    )


def decode_iq(
    iq: np.ndarray,
    fs: float = dsp.DEFAULT_INPUT_FS,
    carrier_hz: float | None = None,
    symbol_lpf_hz: float = dsp.SYMBOL_LPF_HZ,
    costas_alpha: float = 0.3,
    costas_beta: float = 0.005,
    auto_carrier: bool = True,
    progress: Callable[[str], None] | None = None,
    capture_start_monotonic_ns: int | None = None,
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
    bit_rate_drift = (
        dsp.measure_bit_rate_drift(fm, fs) if capture_start_monotonic_ns is not None else None
    )

    carrier_hz = _resolve_carrier(fm, fs, carrier_hz, auto_carrier, emit)

    emit(f"shift_and_filter @ {carrier_hz:.1f} Hz")
    rds_complex = dsp.shift_and_filter(fm, fs, carrier=carrier_hz)

    emit("decimate to 19 kS/s")
    rds_bb = dsp.decimate_to_rds_rate(rds_complex, input_fs=fs)

    # NOTE: coarse_freq_correction was previously called here but its
    # phase-difference estimator behaves like noise for weak real-broadcast
    # BPSK (the dominant case after the biphase fix). A/B testing on 5 known
    # Warsaw stations showed 3× more groups recovered without it. Costas
    # absorbs the residual offset cleanly on its own once AGC is in place.
    freq_off = 0.0

    emit(f"symbol_lpf (cutoff {symbol_lpf_hz} Hz)")
    rds_filtered = dsp.symbol_lpf(rds_bb, dsp.DEFAULT_RDS_FS, cutoff=symbol_lpf_hz)

    emit("agc")
    rds_agc = dsp.agc(rds_filtered)

    emit("costas_loop_bpsk")
    rds_sync = dsp.costas_loop_bpsk(rds_agc, alpha=costas_alpha, beta=costas_beta)

    sps = int(round(dsp.DEFAULT_RDS_FS / dsp.RDS_SYMBOL_RATE))
    emit(f"biphase matched filter + offset search (sps {sps})")
    matched = dsp.biphase_matched_filter(rds_sync, sps_bit=sps)
    attempt = _decode_with_offset_sweep(matched, sps)
    if attempt is None:
        # Very short streams may not have enough biphase samples for the
        # offset sweep. Preserve the legacy paths for those callers.
        emit(f"clock recovery fallback: best_offset + mueller_muller (sps {sps})")
        attempt = _decode_fallback(rds_sync, sps)

    rx_monotonic_ns_by_group = _rx_monotonic_ns_by_group(
        capture_start_monotonic_ns,
        attempt.positions,
        bit_rate_drift,
    )
    if rx_monotonic_ns_by_group is None:
        info = parse_groups(attempt.groups)
    else:
        info = parse_groups(attempt.groups, rx_monotonic_ns_by_group=rx_monotonic_ns_by_group)
    emit(
        f"groups={len(attempt.groups)} clean={attempt.n_clean} "
        f"corrected={attempt.n_corrected} variant={attempt.variant_used} "
        f"freq_off={freq_off:+.1f} Hz"
    )

    return DecodeResult(
        info=info,
        n_groups=len(attempt.groups),
        n_bits=len(attempt.bits),
        freq_offset_hz=float(freq_off),
        symbol_offset=attempt.sym_offset,
        n_groups_clean=attempt.n_clean,
        n_groups_corrected=attempt.n_corrected,
        group_bit_positions=attempt.positions,
    )


def _group_score(
    groups: list[bytearray], n_groups_clean: int, n_groups_corrected: int
) -> tuple[int, int, int]:
    return len(groups), n_groups_clean, -n_groups_corrected


def _best_variant_groups(bits: np.ndarray) -> tuple[list[bytearray], list[int], str, int, int]:
    """Try the four bitstream polarity/order variants and return the one
    that yields the most groups.

    Costas may lock 180° out of phase; differential decoding usually
    handles that, but trying the inverted polarity is a cheap safety net.
    The reversed variants catch rare edge cases where the pipeline
    flips symbol order (mainly relevant for short streams).
    """
    candidates = [
        ("normal", bits, False),
        ("inverted", 1 - bits, False),
        ("reversed", bits[::-1], True),
        ("inv+rev", (1 - bits)[::-1], True),
    ]
    best: list[bytearray] = []
    best_positions: list[int] = []
    best_name = "normal"
    best_clean = 0
    best_corrected = 0
    for name, bstream, reversed_axis in candidates:
        g, positions, n_clean, n_corrected = _find_groups_in_bitstream_with_counts_and_positions(
            np.ascontiguousarray(bstream), tolerate_single_bit=True
        )
        if _group_score(g, n_clean, n_corrected) > _group_score(best, best_clean, best_corrected):
            best = g
            best_positions = _positions_on_capture_axis(len(bits), positions, reversed_axis)
            best_name = name
            best_clean = n_clean
            best_corrected = n_corrected
    return best, best_positions, best_name, best_clean, best_corrected


def _positions_on_capture_axis(n_bits: int, positions: list[int], reversed_axis: bool) -> list[int]:
    if not reversed_axis:
        return positions
    return [n_bits - pos - GROUP_BITS for pos in positions]


def _rx_monotonic_ns_by_group(
    capture_start_monotonic_ns: int | None,
    group_positions: list[int],
    bit_rate_drift: dsp.BitRateDrift | None,
) -> list[int | None] | None:
    if capture_start_monotonic_ns is None or bit_rate_drift is None:
        return None
    bit_rate_hz = bit_rate_drift.bit_rate_hz
    return [
        capture_start_monotonic_ns
        + int((bit_position + GROUP_BITS) / bit_rate_hz * 1e9)
        + dsp.PIPELINE_GROUP_DELAY_NS
        for bit_position in group_positions
    ]


def decode_file(
    path: str,
    fs: float = dsp.DEFAULT_INPUT_FS,
    progress: Callable[[str], None] | None = None,
    fmt: str | None = None,
    capture_start_monotonic_ns: int | None = None,
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
    if capture_start_monotonic_ns is None:
        return decode_iq(iq, fs=fs, progress=progress)
    return decode_iq(
        iq,
        fs=fs,
        progress=progress,
        capture_start_monotonic_ns=capture_start_monotonic_ns,
    )
