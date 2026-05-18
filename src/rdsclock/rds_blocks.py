"""RDS block layer: 26-bit blocks (16 data + 10 CRC), syndromes, offset words.

RDS data-link layer per IEC 62106-2:2021:
    - Each block = 16 data bits + 10 CRC bits.
    - 4 blocks form a "group" (104 bits).
    - CRC generator polynomial: x^10 + x^8 + x^7 + x^5 + x^4 + x^3 + 1  (0x5B9).
    - Each of the 4 block positions has a distinct "offset word" XORed
      with the CRC residue, allowing block-position recovery from
      a sliding bitstream.
    - Block positions 1..4 carry offsets A, B, C, D (or C' instead of C
      for Group Version B).
"""

from collections.abc import Iterable, Sequence

import numpy as np

# CRC generator polynomial (low 10 bits). x^10 + x^8 + x^7 + x^5 + x^4 + x^3 + 1
CRC_POLY = 0x5B9
CRC_BITS = 10
DATA_BITS = 16
BLOCK_BITS = DATA_BITS + CRC_BITS  # 26

# Offset words A, B, C, D, C' (used to identify block position via CRC residue).
# Values from IEC 62106-2:2021 / NRSC-4 RBDS standard.
OFFSET_A = 0x0FC
OFFSET_B = 0x198
OFFSET_C = 0x168
OFFSET_D = 0x1B4
OFFSET_C_PRIME = 0x350

OFFSETS = (OFFSET_A, OFFSET_B, OFFSET_C, OFFSET_D)
OFFSET_BY_NAME = {
    "A": OFFSET_A,
    "B": OFFSET_B,
    "C": OFFSET_C,
    "D": OFFSET_D,
    "C'": OFFSET_C_PRIME,
}

GROUP_BLOCKS = 4
GROUP_BITS = BLOCK_BITS * GROUP_BLOCKS  # 104

_BLOCK_WORD_MASK = (1 << CRC_BITS) - 1
_DATA_WORD_MASK = (1 << DATA_BITS) - 1
_CRC_TOP_BIT = 1 << CRC_BITS
_CRC_POLY_WITH_TOP_BIT = CRC_POLY | _CRC_TOP_BIT
_BLOCK_WEIGHTS = (1 << np.arange(BLOCK_BITS - 1, -1, -1, dtype=np.uint32)).astype(np.uint32)
_DATA_BIT_CORRECTION_MASKS = (1 << np.arange(DATA_BITS - 1, -1, -1, dtype=np.uint32)).astype(
    np.uint32
)


def _build_crc10_table() -> np.ndarray:
    datawords = np.arange(1 << DATA_BITS, dtype=np.uint32)
    reg = np.zeros_like(datawords)
    for i in range(DATA_BITS - 1, -1, -1):
        reg = (reg << 1) | ((datawords >> i) & 1)
        reg = np.where((reg & _CRC_TOP_BIT) != 0, reg ^ _CRC_POLY_WITH_TOP_BIT, reg)
    for _ in range(CRC_BITS):
        reg <<= 1
        reg = np.where((reg & _CRC_TOP_BIT) != 0, reg ^ _CRC_POLY_WITH_TOP_BIT, reg)
    return (reg & _BLOCK_WORD_MASK).astype(np.uint16)


_CRC10_TABLE = _build_crc10_table()


def _crc10_many(datawords: np.ndarray) -> np.ndarray:
    return _CRC10_TABLE[np.asarray(datawords, dtype=np.uint32) & _DATA_WORD_MASK]


def crc10(dataword: int) -> int:
    """Compute the 10-bit RDS CRC of a 16-bit dataword.

    Table-driven equivalent of the standard shift-register implementation:
    feed 16 data bits into a 10-bit register tapped by ``CRC_POLY``, then
    flush 10 zero bits.
    """
    return int(_CRC10_TABLE[int(dataword) & _DATA_WORD_MASK])


def encode_block(dataword: int, offset_word: int) -> int:
    """Build a 26-bit block: 16 data bits followed by (CRC XOR offset_word)."""
    if not 0 <= dataword < (1 << DATA_BITS):
        raise ValueError(f"dataword out of range: 0x{dataword:X}")
    checkword = crc10(dataword) ^ offset_word
    return ((dataword & 0xFFFF) << CRC_BITS) | (checkword & 0x3FF)


def block_dataword(block26: int) -> int:
    return (block26 >> CRC_BITS) & 0xFFFF


def block_checkword(block26: int) -> int:
    return block26 & _BLOCK_WORD_MASK


def _block_syndrome(block26: int, offset_word: int) -> int:
    data = block_dataword(block26)
    return block_checkword(block26) ^ offset_word ^ crc10(data)


def _build_single_bit_syndrome_table() -> dict[int, int]:
    table: dict[int, int] = {}
    clean = encode_block(0, OFFSET_A)
    for pos in range(BLOCK_BITS):
        corrupted = clean ^ (1 << (BLOCK_BITS - 1 - pos))
        syndrome = _block_syndrome(corrupted, OFFSET_A)
        if syndrome == 0 or syndrome in table:
            raise RuntimeError(f"non-unique RDS single-bit syndrome at position {pos}")
        table[syndrome] = pos
    return table


_SINGLE_BIT_SYNDROME_TABLE = _build_single_bit_syndrome_table()
_SINGLE_BIT_SYNDROME_LOOKUP = np.full(1 << CRC_BITS, -1, dtype=np.int8)
for _syndrome, _position in _SINGLE_BIT_SYNDROME_TABLE.items():
    _SINGLE_BIT_SYNDROME_LOOKUP[_syndrome] = _position


def _single_bit_error_position(block26: int, offset_word: int) -> int:
    return _SINGLE_BIT_SYNDROME_TABLE.get(_block_syndrome(block26, offset_word), -1)


def block_valid(
    block26: int,
    block_no: int,
    version_b: bool | None = None,
    correct_single_bit: bool = False,
) -> bool:
    """Return True if a 26-bit word is a valid block at the given position 0..3.

    For block C (index 2):
      - ``version_b=False`` requires offset C
      - ``version_b=True``  requires offset C'
      - ``version_b=None``  accepts either (back-compat for callers
        that do not yet know which group version this is).
    """
    if not 0 <= block_no < GROUP_BLOCKS:
        raise ValueError(f"block_no out of [0..3]: {block_no}")
    if block_no == 2:
        if version_b is True:
            offset_words = (OFFSET_C_PRIME,)
        elif version_b is False:
            offset_words = (OFFSET_C,)
        else:
            offset_words = (OFFSET_C, OFFSET_C_PRIME)
    else:
        offset_words = (OFFSETS[block_no],)

    for offset_word in offset_words:
        if _block_syndrome(block26, offset_word) == 0:
            return True

    if correct_single_bit:
        for offset_word in offset_words:
            if _single_bit_error_position(block26, offset_word) >= 0:
                return True

    return False


def encode_group(words: Sequence[int], version_b: bool = False) -> list[int]:
    """Encode 4 × 16-bit words as 4 × 26-bit blocks (with their offset words)."""
    if len(words) != GROUP_BLOCKS:
        raise ValueError(f"expected {GROUP_BLOCKS} words, got {len(words)}")
    offsets = list(OFFSETS)
    if version_b:
        offsets[2] = OFFSET_C_PRIME
    return [encode_block(w, off) for w, off in zip(words, offsets, strict=True)]


def blocks_to_bits(blocks: Iterable[int]) -> np.ndarray:
    """Concatenate a list of 26-bit blocks into a single uint8 ndarray of bits."""
    out: list[int] = []
    for block in blocks:
        for i in range(BLOCK_BITS - 1, -1, -1):
            out.append((block >> i) & 1)
    return np.asarray(out, dtype=np.uint8)


def bits_to_word(bits: Sequence[int]) -> int:
    """Pack a bit sequence into an int (MSB first)."""
    bit_array = np.asarray(bits, dtype=np.uint8)
    if bit_array.size == BLOCK_BITS:
        return int(_bits_to_words_26(bit_array))
    word = 0
    for b in bit_array:
        word = (word << 1) | (int(b) & 1)
    return word


def _bits_to_words_26(bit_windows: np.ndarray, assume_binary: bool = False) -> np.ndarray:
    bit_windows = np.asarray(bit_windows, dtype=np.uint8)
    if not assume_binary:
        bit_windows = np.bitwise_and(bit_windows, 1)
    return bit_windows @ _BLOCK_WEIGHTS


def _coerce_bits(bits: np.ndarray) -> np.ndarray:
    if not isinstance(bits, np.ndarray):
        bits = np.asarray(bits, dtype=np.uint8)
    if bits.dtype != np.uint8:
        bits = bits.astype(np.uint8)
    return np.bitwise_and(bits, 1)


def _correct_datawords(datawords: np.ndarray, error_positions: np.ndarray) -> np.ndarray:
    corrected = np.asarray(datawords, dtype=np.uint32).copy()
    data_errors = (error_positions >= 0) & (error_positions < DATA_BITS)
    if np.any(data_errors):
        corrected[data_errors] ^= _DATA_BIT_CORRECTION_MASKS[error_positions[data_errors]]
    return corrected


def _drop_corrected_starts_overlapping_clean(
    group_starts: np.ndarray, correction_counts: np.ndarray
) -> np.ndarray:
    clean_starts = group_starts[correction_counts[group_starts] == 0]
    corrected_starts = group_starts[correction_counts[group_starts] > 0]
    if len(clean_starts) == 0 or len(corrected_starts) == 0:
        return group_starts

    left = np.searchsorted(clean_starts, corrected_starts - GROUP_BITS + 1, side="left")
    right = np.searchsorted(clean_starts, corrected_starts + GROUP_BITS, side="left")
    corrected_without_clean_overlap = corrected_starts[left == right]
    return np.sort(np.concatenate((clean_starts, corrected_without_clean_overlap)))


def _find_groups_in_bitstream_with_counts_and_positions(
    bits: np.ndarray, tolerate_single_bit: bool = False
) -> tuple[list[bytearray], list[int], int, int]:
    """Slide over a bitstream and extract groups of 4 consecutively valid blocks.

    Enforces version consistency: if block B advertises Version A,
    block C must use offset C; for Version B, block 3 must use offset C'.
    This significantly reduces false-positive group matches on weak streams.

    Returns 8-byte bytearrays, their bitstream start positions, and
    clean/corrected group counters.
    """
    bits = _coerce_bits(bits)
    n = len(bits)
    groups: list[bytearray] = []
    positions: list[int] = []
    if n < GROUP_BITS:
        return groups, positions, 0, 0

    windows = np.lib.stride_tricks.sliding_window_view(bits, BLOCK_BITS)
    words = _bits_to_words_26(windows, assume_binary=True)
    data = (words >> CRC_BITS) & _DATA_WORD_MASK
    check = words & _BLOCK_WORD_MASK
    expected = _crc10_many(data)

    valid_a = (check ^ OFFSET_A) == expected
    valid_b = (check ^ OFFSET_B) == expected
    valid_c = (check ^ OFFSET_C) == expected
    valid_c_prime = (check ^ OFFSET_C_PRIME) == expected
    valid_d = (check ^ OFFSET_D) == expected

    start_count = n - GROUP_BITS + 1
    block_b_slice = slice(BLOCK_BITS, BLOCK_BITS + start_count)
    block_b_data = data[block_b_slice]
    correction_counts = np.zeros(start_count, dtype=np.uint8)
    error_positions_by_block: tuple[np.ndarray, ...] = ()

    if tolerate_single_bit:
        syndrome_a = check ^ OFFSET_A ^ expected
        syndrome_b = check ^ OFFSET_B ^ expected
        syndrome_c = check ^ OFFSET_C ^ expected
        syndrome_c_prime = check ^ OFFSET_C_PRIME ^ expected
        syndrome_d = check ^ OFFSET_D ^ expected

        error_pos_a_all = _SINGLE_BIT_SYNDROME_LOOKUP[syndrome_a]
        error_pos_b_all = _SINGLE_BIT_SYNDROME_LOOKUP[syndrome_b]
        error_pos_c_all = _SINGLE_BIT_SYNDROME_LOOKUP[syndrome_c]
        error_pos_c_prime_all = _SINGLE_BIT_SYNDROME_LOOKUP[syndrome_c_prime]
        error_pos_d_all = _SINGLE_BIT_SYNDROME_LOOKUP[syndrome_d]

        error_pos_a = error_pos_a_all[:start_count]
        error_pos_b = error_pos_b_all[block_b_slice]
        block_b_data = _correct_datawords(block_b_data, error_pos_b)
    else:
        error_pos_a = np.empty(0, dtype=np.int8)
        error_pos_b = np.empty(0, dtype=np.int8)

    version_b = ((block_b_data >> 11) & 1).astype(bool)
    block_c_valid = np.where(
        version_b,
        valid_c_prime[2 * BLOCK_BITS : 2 * BLOCK_BITS + start_count],
        valid_c[2 * BLOCK_BITS : 2 * BLOCK_BITS + start_count],
    )
    if tolerate_single_bit:
        error_pos_c = np.where(
            version_b,
            error_pos_c_prime_all[2 * BLOCK_BITS : 2 * BLOCK_BITS + start_count],
            error_pos_c_all[2 * BLOCK_BITS : 2 * BLOCK_BITS + start_count],
        )
        error_pos_d = error_pos_d_all[3 * BLOCK_BITS : 3 * BLOCK_BITS + start_count]
        block_a_clean = valid_a[:start_count]
        block_b_clean = valid_b[block_b_slice]
        block_d_clean = valid_d[3 * BLOCK_BITS : 3 * BLOCK_BITS + start_count]
        block_a_corrected = error_pos_a >= 0
        block_b_corrected = error_pos_b >= 0
        block_c_corrected = error_pos_c >= 0
        block_d_corrected = error_pos_d >= 0
        correction_counts = (
            block_a_corrected.astype(np.uint8)
            + block_b_corrected.astype(np.uint8)
            + block_c_corrected.astype(np.uint8)
            + block_d_corrected.astype(np.uint8)
        )
        group_starts = np.flatnonzero(
            (block_a_clean | block_a_corrected)
            & (block_b_clean | block_b_corrected)
            & (block_c_valid | block_c_corrected)
            & (block_d_clean | block_d_corrected)
            & (correction_counts <= 1)
        )
        group_starts = _drop_corrected_starts_overlapping_clean(group_starts, correction_counts)
        error_positions_by_block = (error_pos_a, error_pos_b, error_pos_c, error_pos_d)
    else:
        group_starts = np.flatnonzero(
            valid_a[:start_count]
            & valid_b[block_b_slice]
            & block_c_valid
            & valid_d[3 * BLOCK_BITS : 3 * BLOCK_BITS + start_count]
        )

    next_scan_start = 0
    n_groups_clean = 0
    n_groups_corrected = 0
    group_offsets = np.array([0, BLOCK_BITS, 2 * BLOCK_BITS, 3 * BLOCK_BITS], dtype=np.intp)
    for start in group_starts:
        group_start = int(start)
        if group_start >= next_scan_start:
            datawords = data[group_start + group_offsets].astype(np.uint32, copy=True)
            corrected_count = int(correction_counts[group_start])
            if corrected_count:
                for idx, error_positions in enumerate(error_positions_by_block):
                    error_pos = int(error_positions[group_start])
                    if 0 <= error_pos < DATA_BITS:
                        datawords[idx] ^= _DATA_BIT_CORRECTION_MASKS[error_pos]
                n_groups_corrected += 1
            else:
                n_groups_clean += 1
            buf = bytearray(8)
            for idx, dw_raw in enumerate(datawords):
                dw = int(dw_raw)
                buf[idx * 2] = (dw >> 8) & 0xFF
                buf[idx * 2 + 1] = dw & 0xFF
            groups.append(buf)
            positions.append(group_start)
            next_scan_start = group_start + GROUP_BITS
    return groups, positions, n_groups_clean, n_groups_corrected


def _find_groups_in_bitstream_with_counts(
    bits: np.ndarray, tolerate_single_bit: bool = False
) -> tuple[list[bytearray], int, int]:
    groups, _, n_groups_clean, n_groups_corrected = (
        _find_groups_in_bitstream_with_counts_and_positions(bits, tolerate_single_bit)
    )
    return groups, n_groups_clean, n_groups_corrected


def find_groups_in_bitstream_with_positions(
    bits: np.ndarray,
    *,
    tolerate_single_bit: bool = False,
) -> tuple[list[bytearray], list[int]]:
    """Like :func:`find_groups_in_bitstream`, also returning group start bit indexes."""
    groups, positions, _, _ = _find_groups_in_bitstream_with_counts_and_positions(
        bits, tolerate_single_bit
    )
    return groups, positions


def find_groups_in_bitstream(
    bits: np.ndarray, tolerate_single_bit: bool = False
) -> list[bytearray]:
    """Slide over a bitstream and extract groups of 4 consecutively valid blocks.

    By default every block must pass CRC exactly. When ``tolerate_single_bit``
    is true, a group may contain one block whose syndrome identifies a single
    flipped bit; groups requiring two or more block corrections are dropped.
    """
    groups, _, _ = _find_groups_in_bitstream_with_counts(bits, tolerate_single_bit)
    return groups


def count_valid_blocks(bits: np.ndarray) -> int:
    """Diagnostic: count 26-bit windows that pass CRC for any block position 0..3."""
    bits = _coerce_bits(bits)
    total = 0
    for i in range(0, len(bits) - BLOCK_BITS + 1, BLOCK_BITS):
        word = bits_to_word(bits[i : i + BLOCK_BITS])
        if any(block_valid(word, blk) for blk in range(GROUP_BLOCKS)):
            total += 1
    return total


def differential_decode(bits: np.ndarray) -> np.ndarray:
    """Inverse of differential encoding: ``data_bit[n] = tx_bit[n] XOR tx_bit[n-1]``.

    The first transmitted bit is the seed and cannot be recovered; the
    returned array has length ``len(bits) - 1``.
    """
    bits = np.asarray(bits, dtype=np.uint8)
    if len(bits) < 2:
        return np.zeros(0, dtype=np.uint8)
    return np.bitwise_xor(bits[1:], bits[:-1])


def differential_encode(data_bits: np.ndarray, seed: int = 0) -> np.ndarray:
    """Apply RDS differential encoding: ``tx[n] = data[n] XOR tx[n-1]``.

    Returns an array of ``len(data_bits) + 1`` bits, where the first bit
    is the chosen seed (typically 0).
    """
    data_bits = np.asarray(data_bits, dtype=np.uint8)
    out = np.empty(len(data_bits) + 1, dtype=np.uint8)
    out[0] = seed & 1
    for i, b in enumerate(data_bits):
        out[i + 1] = out[i] ^ (int(b) & 1)
    return out


def group_bytes_to_words(group: Sequence[int]) -> tuple[int, int, int, int]:
    """Decompose an 8-byte buffer into (block_a, block_b, block_c, block_d)."""
    if len(group) < 8:
        raise ValueError(f"group must be 8 bytes, got {len(group)}")
    a = (group[0] << 8) | group[1]
    b = (group[2] << 8) | group[3]
    c = (group[4] << 8) | group[5]
    d = (group[6] << 8) | group[7]
    return a, b, c, d


def group_words_to_bytes(words: Sequence[int]) -> bytearray:
    """Inverse of :func:`group_bytes_to_words`."""
    if len(words) != 4:
        raise ValueError(f"expected 4 words, got {len(words)}")
    out = bytearray(8)
    for i, w in enumerate(words):
        out[i * 2] = (w >> 8) & 0xFF
        out[i * 2 + 1] = w & 0xFF
    return out
