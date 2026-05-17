"""Tests for MJD + encode/decode of 4A Clock Time groups."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from rdsclock.rds_clock import (
    MJD_EPOCH,
    ClockTime,
    datetime_to_mjd,
    decode_clock_time,
    encode_clock_time,
    mjd_to_date,
)


class TestMjd:
    def test_epoch_zero(self):
        assert datetime_to_mjd(MJD_EPOCH) == 0
        assert mjd_to_date(0) == MJD_EPOCH

    def test_known_dates(self):
        # MJD 2000-01-01 = 51544 (verified against US Naval Observatory)
        dt = datetime(2000, 1, 1, tzinfo=UTC)
        assert datetime_to_mjd(dt) == 51544
        # Round-trip a far-future date to confirm the conversion remains
        # consistent across the full MJD range we accept (~1900..2100).
        dt2 = datetime(2050, 6, 15, tzinfo=UTC)
        assert mjd_to_date(datetime_to_mjd(dt2)) == dt2

    def test_roundtrip(self):
        for year in [1900, 1950, 2000, 2025, 2050, 2099]:
            for month in [1, 6, 12]:
                dt = datetime(year, month, 15, tzinfo=UTC)
                mjd = datetime_to_mjd(dt)
                back = mjd_to_date(mjd)
                assert back == dt

    def test_ignores_seconds(self):
        # datetime_to_mjd returns only the day (time is separate).
        dt = datetime(2026, 5, 16, 14, 23, 45, tzinfo=UTC)
        assert datetime_to_mjd(dt) == datetime_to_mjd(datetime(2026, 5, 16, tzinfo=UTC))


def _full_block_b(extra: int, pty: int = 0, tp: bool = False) -> int:
    """Assemble a full block B from the extra CT payload (MJD[16:15] in bits 1..0)."""
    return (4 << 12) | (0 << 11) | ((1 if tp else 0) << 10) | ((pty & 0x1F) << 5) | (extra & 0x3)


class TestEncodeDecodeClockTime:
    def test_basic_utc(self):
        dt_local = datetime(2026, 5, 16, 14, 30, tzinfo=UTC)
        b_extra, c, d = encode_clock_time(dt_local, local_offset_minutes=0)
        b = _full_block_b(b_extra)
        decoded = decode_clock_time(b, c, d)
        assert decoded is not None
        assert decoded.utc == dt_local
        assert decoded.local_offset_minutes == 0

    def test_positive_offset(self):
        # Local time 2026-05-16 16:30 with offset +120 min = UTC 14:30
        tz = timezone(timedelta(minutes=120))
        dt_local = datetime(2026, 5, 16, 16, 30, tzinfo=tz)
        b_extra, c, d = encode_clock_time(dt_local)
        decoded = decode_clock_time(_full_block_b(b_extra), c, d)
        assert decoded is not None
        assert decoded.utc == datetime(2026, 5, 16, 14, 30, tzinfo=UTC)
        assert decoded.local_offset_minutes == 120
        assert decoded.local == dt_local

    def test_negative_offset(self):
        tz = timezone(timedelta(minutes=-150))  # -2:30
        dt_local = datetime(2026, 5, 16, 7, 0, tzinfo=tz)
        b_extra, c, d = encode_clock_time(dt_local)
        decoded = decode_clock_time(_full_block_b(b_extra), c, d)
        assert decoded is not None
        assert decoded.local_offset_minutes == -150
        assert decoded.local == dt_local

    def test_roundtrip_many_times(self):
        base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        for hours_ahead in range(0, 24 * 365 * 2, 47):
            dt = base + timedelta(hours=hours_ahead)
            b_extra, c, d = encode_clock_time(dt, local_offset_minutes=0)
            decoded = decode_clock_time(_full_block_b(b_extra), c, d)
            assert decoded is not None, f"failed for {dt}"
            assert decoded.utc == dt, f"mismatch for {dt}: got {decoded.utc}"

    def test_invalid_offset_minutes(self):
        dt = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)
        with pytest.raises(ValueError):
            encode_clock_time(dt, local_offset_minutes=15)

    def test_offset_out_of_range(self):
        dt = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)
        with pytest.raises(ValueError):
            encode_clock_time(dt, local_offset_minutes=24 * 60)  # 24h = mag 48 > 31

    def test_decode_mjd_out_of_range(self):
        # MJD = 100000 -> year ~2132, outside our reasonable range.
        # block_b = MJD[16:15] = 11 → 3, block_c = (MJD[14:0] << 1) | hour[4]
        big_mjd = 100000
        b_extra = (big_mjd >> 15) & 0x3
        c = ((big_mjd & 0x7FFF) << 1) | 0
        d = (12 << 12) | (30 << 6) | 0  # hour=12, minute=30
        assert decode_clock_time(_full_block_b(b_extra), c, d) is None

    def test_decode_clearly_invalid_hour(self):
        # hour=25 -> invalid (HOUR[4]=1, HOUR[3:0]=1001 -> 25)
        mjd = 60000
        b_extra = (mjd >> 15) & 0x3
        c = ((mjd & 0x7FFF) << 1) | ((25 >> 4) & 0x1)
        d = ((25 & 0xF) << 12) | (0 << 6) | 0
        assert decode_clock_time(_full_block_b(b_extra), c, d) is None


class TestClockTimeStr:
    def test_str_format(self):
        dt = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        ct = ClockTime(utc=dt, local_offset_minutes=120)
        s = str(ct)
        assert "2026-05-16" in s
        assert "12:00" in s
        assert "+02:00" in s
