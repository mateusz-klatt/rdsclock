"""Tests for the block layer: CRC, block encode/decode, syndromes, bitstream."""

import numpy as np
import pytest

import rdsclock.rds_blocks as rds_blocks
from rdsclock.rds_blocks import (
    _SINGLE_BIT_SYNDROME_TABLE,
    BLOCK_BITS,
    DATA_BITS,
    GROUP_BITS,
    OFFSET_A,
    OFFSET_C,
    OFFSET_C_PRIME,
    OFFSETS,
    _bits_to_words_26,
    _crc10_many,
    _drop_corrected_starts_overlapping_clean,
    _find_groups_in_bitstream_with_counts,
    bits_to_word,
    block_dataword,
    block_valid,
    blocks_to_bits,
    crc10,
    differential_decode,
    differential_encode,
    encode_block,
    encode_group,
    find_groups_in_bitstream,
    group_bytes_to_words,
    group_words_to_bytes,
)


def _crc10_reference(dataword: int) -> int:
    reg = 0
    for i in range(DATA_BITS - 1, -1, -1):
        reg = (reg << 1) | ((dataword >> i) & 1)
        if reg & (1 << 10):
            reg ^= 0x5B9 | (1 << 10)
    for _ in range(10):
        reg <<= 1
        if reg & (1 << 10):
            reg ^= 0x5B9 | (1 << 10)
    return reg & ((1 << 10) - 1)


class TestCrc10:
    def test_zero_input(self):
        assert crc10(0) == 0

    def test_idempotent(self):
        # CRC should be deterministic.
        assert crc10(0x1234) == crc10(0x1234)

    def test_range(self):
        # CRC fits in 10 bits.
        for v in [0x0000, 0xFFFF, 0xABCD, 0x5555, 0xAAAA]:
            r = crc10(v)
            assert 0 <= r < (1 << 10)

    def test_difference_propagates(self):
        # A single data-bit change alters the CRC.
        a = crc10(0x1234)
        b = crc10(0x1235)
        assert a != b

    def test_matches_shift_register_for_full_u16_space(self):
        expected = np.fromiter(
            (_crc10_reference(v) for v in range(1 << DATA_BITS)),
            dtype=np.uint16,
            count=1 << DATA_BITS,
        )
        actual = _crc10_many(np.arange(1 << DATA_BITS, dtype=np.uint32))
        np.testing.assert_array_equal(actual, expected)
        assert crc10(0x1FFFF) == int(expected[-1])


class TestEncodeBlock:
    def test_roundtrip(self):
        for data in [0x0000, 0xFFFF, 0xCAFE, 0x1234]:
            for off in OFFSETS:
                blk = encode_block(data, off)
                assert 0 <= blk < (1 << BLOCK_BITS)
                assert block_dataword(blk) == data

    def test_block_valid_each_offset(self):
        # A block encoded with offset A should be valid as block_no=0,
        # but NOT as 1/2/3 (at least for random data).
        np.random.seed(42)
        for _ in range(20):
            data = int(np.random.randint(0, 1 << DATA_BITS))
            for blk_no, off in enumerate(OFFSETS):
                blk = encode_block(data, off)
                assert block_valid(blk, blk_no)

    def test_invalid_offset_fails(self):
        data = 0xBEEF
        blk = encode_block(data, OFFSET_A)
        # Low collision chance: an A block should not be valid as B/D (different CRC).
        # (C can sometimes be valid via C', so we skip it in this test.)
        assert not block_valid(blk, 1)  # B
        assert not block_valid(blk, 3)  # D

    def test_bit_error_detected(self):
        data = 0xBEEF
        for off in OFFSETS:
            blk = encode_block(data, off)
            for bit_to_flip in range(BLOCK_BITS):
                corrupted = blk ^ (1 << bit_to_flip)
                assert not block_valid(corrupted, OFFSETS.index(off))

    def test_c_prime_accepted_for_block_c(self):
        data = 0xC0DE
        blk_c = encode_block(data, OFFSET_C)
        blk_c_prime = encode_block(data, OFFSET_C_PRIME)
        assert block_valid(blk_c, 2)
        assert block_valid(blk_c_prime, 2)

    def test_single_bit_syndrome_table_covers_block_positions(self):
        assert len(_SINGLE_BIT_SYNDROME_TABLE) == BLOCK_BITS
        assert set(_SINGLE_BIT_SYNDROME_TABLE.values()) == set(range(BLOCK_BITS))
        assert 0 not in _SINGLE_BIT_SYNDROME_TABLE

    def test_single_bit_syndrome_table_rejects_collisions(self, monkeypatch):
        monkeypatch.setattr(rds_blocks, "_block_syndrome", lambda block, offset: 1)
        with pytest.raises(RuntimeError, match="non-unique"):
            rds_blocks._build_single_bit_syndrome_table()

    def test_block_valid_can_accept_one_single_bit_error_when_requested(self):
        data = 0xBEEF
        blk = encode_block(data, OFFSET_A)
        corrupted_data = blk ^ (1 << (BLOCK_BITS - 1 - 3))
        corrupted_crc = blk ^ (1 << (BLOCK_BITS - 1 - 20))
        corrupted_double = corrupted_data ^ (1 << (BLOCK_BITS - 1 - 20))

        assert not block_valid(corrupted_data, 0)
        assert block_valid(corrupted_data, 0, correct_single_bit=True)
        assert block_valid(corrupted_crc, 0, correct_single_bit=True)
        assert not block_valid(corrupted_double, 0, correct_single_bit=True)

        blk_c_prime = encode_block(data, OFFSET_C_PRIME)
        corrupted_c_prime = blk_c_prime ^ (1 << (BLOCK_BITS - 1 - 4))
        assert block_valid(corrupted_c_prime, 2, correct_single_bit=True)


class TestEncodeGroup:
    def test_basic_a_version(self):
        words = (0xCAFE, 0x4000, 0x1234, 0x5678)
        blocks = encode_group(words)
        assert len(blocks) == 4
        for blk_no, blk in enumerate(blocks):
            assert block_valid(blk, blk_no)

    def test_b_version_uses_c_prime(self):
        words = (0xCAFE, 0x4800, 0xABCD, 0x1357)
        blocks = encode_group(words, version_b=True)
        # Block 2 is valid (via C' inside block_valid).
        for blk_no, blk in enumerate(blocks):
            assert block_valid(blk, blk_no)


class TestBitstream:
    def test_blocks_to_bits_length(self):
        words = (0xCAFE, 0x4000, 0x1234, 0x5678)
        blocks = encode_group(words)
        bits = blocks_to_bits(blocks)
        assert len(bits) == GROUP_BITS  # 104

    def test_bits_to_word_msb_first(self):
        # 16-bit word 0b1100110011001100 = 0xCCCC
        bits = [1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0]
        assert bits_to_word(bits) == 0xCCCC

    def test_bits_to_word_26_uses_vectorized_weights(self):
        bits = np.array(
            [1, 0, 1, 1, 0, 0, 1, 0, 1, 0, 1, 0, 1, 1, 1, 0, 0, 1, 0, 1, 1, 0, 1, 0, 0, 1],
            dtype=np.uint8,
        )
        assert bits_to_word(bits) == 0b10110010101011100101101001
        np.testing.assert_array_equal(_bits_to_words_26(bits[None, :]), [bits_to_word(bits)])

    def test_find_groups_in_clean_bitstream(self):
        # Encode 3 groups with different data, then find them all.
        all_blocks = []
        for tag in range(3):
            words = (0xCAFE + tag, 0x4000, 0x1234, 0x5678)
            all_blocks.extend(encode_group(words))
        bits = blocks_to_bits(all_blocks)
        # Add garbage at the beginning and end to force scanning.
        padding = np.zeros(50, dtype=np.uint8)
        noisy = np.concatenate([padding, bits, padding])

        groups = find_groups_in_bitstream(noisy)
        assert len(groups) == 3
        for tag, g in enumerate(groups):
            a, b, c, d = group_bytes_to_words(g)
            assert a == 0xCAFE + tag
            assert b == 0x4000
            assert c == 0x1234
            assert d == 0x5678

    def test_find_groups_skips_bit_errors(self):
        # Encode 2 groups; corrupt the middle bits - the first should be
        # rejected, while the second should still be found.
        words1 = (0xCAFE, 0x4000, 0x1234, 0x5678)
        words2 = (0xBABE, 0x4000, 0x9ABC, 0xDEF0)
        blocks = encode_group(words1) + encode_group(words2)
        bits = blocks_to_bits(blocks).copy()
        # Corrupt one bit in the first group (bit 10).
        bits[10] ^= 1
        groups = find_groups_in_bitstream(bits)
        # The second group is still OK.
        assert len(groups) >= 1
        for g in groups:
            a, _, _, _ = group_bytes_to_words(g)
            assert a in (0xBABE, 0xCAFE)

    def test_find_groups_short_stream_returns_empty(self):
        assert find_groups_in_bitstream(np.zeros(GROUP_BITS - 1, dtype=np.uint8)) == []

    def test_find_groups_can_recover_one_corrupted_data_bit(self):
        words = (0xCAFE, 0x4000, 0x1234, 0x5678)
        bits = blocks_to_bits(encode_group(words)).copy()
        bits[BLOCK_BITS + 4] ^= 1  # block B version bit: raw data would select C'.

        assert find_groups_in_bitstream(bits) == []
        groups, n_clean, n_corrected = _find_groups_in_bitstream_with_counts(
            bits, tolerate_single_bit=True
        )

        assert n_clean == 0
        assert n_corrected == 1
        assert len(groups) == 1
        assert group_bytes_to_words(groups[0]) == words

    def test_find_groups_can_recover_one_corrupted_crc_bit_without_changing_data(self):
        words = (0xCAFE, 0x4000, 0x1234, 0x5678)
        bits = blocks_to_bits(encode_group(words)).copy()
        bits[3 * BLOCK_BITS + 20] ^= 1

        groups = find_groups_in_bitstream(bits, tolerate_single_bit=True)

        assert len(groups) == 1
        assert group_bytes_to_words(groups[0]) == words

    def test_find_groups_drops_candidates_requiring_two_corrections(self):
        bits = blocks_to_bits(encode_group((0xCAFE, 0x4000, 0x1234, 0x5678))).copy()
        bits[3] ^= 1
        bits[3 * BLOCK_BITS + 20] ^= 1

        groups, n_clean, n_corrected = _find_groups_in_bitstream_with_counts(
            bits, tolerate_single_bit=True
        )

        assert groups == []
        assert n_clean == 0
        assert n_corrected == 0

    def test_corrected_group_starts_do_not_displace_overlapping_clean_starts(self):
        correction_counts = np.zeros(400, dtype=np.uint8)
        correction_counts[[40, 260]] = 1
        starts = np.array([40, 120, 260], dtype=np.intp)

        filtered = _drop_corrected_starts_overlapping_clean(starts, correction_counts)

        np.testing.assert_array_equal(filtered, np.array([120, 260], dtype=np.intp))
        np.testing.assert_array_equal(
            _drop_corrected_starts_overlapping_clean(
                np.array([40], dtype=np.intp), correction_counts
            ),
            np.array([40], dtype=np.intp),
        )

    def test_find_groups_tolerant_mode_rejects_random_noise_smoke(self):
        """Single-bit correction must not turn random noise into many fake groups."""
        noise = np.random.RandomState(42).randint(0, 2, size=10_000).astype(np.uint8)
        groups, n_clean, n_corrected = _find_groups_in_bitstream_with_counts(
            noise, tolerate_single_bit=True
        )

        assert len(groups) < 10
        assert n_clean + n_corrected == len(groups)


class TestDifferential:
    def test_roundtrip(self):
        data = np.array([1, 0, 1, 1, 0, 0, 1, 0, 1, 1], dtype=np.uint8)
        tx = differential_encode(data, seed=0)
        rx = differential_decode(tx)
        np.testing.assert_array_equal(rx, data)

    def test_polarity_invariant(self):
        # Differential decoding should work regardless of stream inversion.
        data = np.array([1, 0, 1, 1, 0, 1, 0, 0, 1], dtype=np.uint8)
        tx = differential_encode(data, seed=0)
        rx = differential_decode(1 - tx)  # global inversion
        np.testing.assert_array_equal(rx, data)


class TestGroupBytes:
    def test_roundtrip(self):
        words = (0xCAFE, 0xBABE, 0xDEAD, 0xBEEF)
        bytes_ = group_words_to_bytes(words)
        assert len(bytes_) == 8
        roundtrip = group_bytes_to_words(bytes_)
        assert roundtrip == words
