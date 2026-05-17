"""Testy parsera i encodera grup RDS (PS, RT, CT)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from rdsclock.rds_groups import (
    StationInfo,
    encode_group_0a,
    encode_group_4a,
    encode_ps_groups,
    parse_group,
    parse_groups,
)


class TestPsParsing:
    def test_single_segment(self):
        g = encode_group_0a(pi=0xABCD, ps_segment_index=0, ps_chars="RM")
        info = StationInfo()
        parse_group(g, info)
        assert info.pi == 0xABCD
        assert info.ps_name == "RM"

    def test_all_segments(self):
        groups = encode_ps_groups(pi=0x3F44, ps_name="RMF FM  ")
        info = parse_groups(groups)
        assert info.pi == 0x3F44
        assert info.ps_name == "RMF FM"

    def test_short_name_padded(self):
        groups = encode_ps_groups(pi=0x1234, ps_name="ABC")
        info = parse_groups(groups)
        assert info.ps_name == "ABC"

    def test_full_eight_chars(self):
        groups = encode_ps_groups(pi=0x4321, ps_name="RadioZET")
        info = parse_groups(groups)
        assert info.ps_name == "RadioZET"

    def test_group_count(self):
        groups = encode_ps_groups(pi=0x1111, ps_name="HELLO   ")
        info = parse_groups(groups)
        assert info.group_counts.get("0A", 0) == 4


class TestCtParsing:
    def test_utc_roundtrip(self):
        dt = datetime(2026, 5, 16, 14, 30, tzinfo=UTC)
        g = encode_group_4a(pi=0xCAFE, clock_time_local=dt)
        info = StationInfo()
        parse_group(g, info)
        assert info.pi == 0xCAFE
        assert info.clock_times
        ct = info.latest_clock
        assert ct is not None
        assert ct.utc == dt

    def test_local_offset(self):
        tz = timezone(timedelta(minutes=120))
        dt_local = datetime(2026, 5, 16, 16, 30, tzinfo=tz)
        g = encode_group_4a(pi=0xCAFE, clock_time_local=dt_local)
        info = parse_groups([g])
        ct = info.latest_clock
        assert ct is not None
        assert ct.local_offset_minutes == 120
        assert ct.local == dt_local

    def test_multiple_cts_accumulated(self):
        base = datetime(2026, 5, 16, 14, 0, tzinfo=UTC)
        groups = [
            encode_group_4a(pi=0xCAFE, clock_time_local=base + timedelta(minutes=m))
            for m in [0, 1, 2, 3]
        ]
        info = parse_groups(groups)
        assert len(info.clock_times) == 4
        assert info.latest_clock.utc.minute == 3

    def test_mixed_with_ps(self):
        ps_groups = encode_ps_groups(pi=0xCAFE, ps_name="TESTSDR ")
        ct = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        ct_group = encode_group_4a(pi=0xCAFE, clock_time_local=ct)
        info = parse_groups(ps_groups + [ct_group] + ps_groups)
        assert info.ps_name == "TESTSDR"
        assert info.latest_clock.utc == ct
        assert info.group_counts["0A"] == 8
        assert info.group_counts["4A"] == 1


class TestEncoders:
    def test_encode_group_0a_validations(self):
        with pytest.raises(ValueError):
            encode_group_0a(pi=0, ps_segment_index=4, ps_chars="XY")
        with pytest.raises(ValueError):
            encode_group_0a(pi=0, ps_segment_index=0, ps_chars="XYZ")

    def test_encode_ps_groups_length(self):
        gs = encode_ps_groups(pi=0xDEAD, ps_name="HELLO")
        assert len(gs) == 4
        for g in gs:
            assert len(g) == 8
