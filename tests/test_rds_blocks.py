"""Tests for the block layer: CRC, block encode/decode, syndromes, bitstream."""

import numpy as np

from rdsclock.rds_blocks import (
    BLOCK_BITS,
    DATA_BITS,
    GROUP_BITS,
    OFFSET_A,
    OFFSET_C,
    OFFSET_C_PRIME,
    OFFSETS,
    _bits_to_words_26,
    _crc10_many,
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
