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


def _validated_ps_groups(pi: int, ps_name: str) -> list[bytearray]:
    groups = encode_ps_groups(pi=pi, ps_name=ps_name)
    return groups + groups


class TestPsParsing:
    def test_single_segment(self):
        g = encode_group_0a(pi=0xABCD, ps_segment_index=0, ps_chars="RM")
        info = StationInfo()
        parse_group(g, info)
        assert info.pi == 0xABCD
        assert info.ps_name == ""
        assert info.latest_ps_candidate == ""

    def test_all_segments(self):
        groups = _validated_ps_groups(pi=0x3F44, ps_name="RMF FM  ")
        info = parse_groups(groups)
        assert info.pi == 0x3F44
        assert info.ps_name == "RMF FM"
        assert info.validated_ps_name == "RMF FM"
        assert info.latest_ps_candidate == "RMF FM  "

    def test_short_name_padded(self):
        groups = _validated_ps_groups(pi=0x1234, ps_name="ABC")
        info = parse_groups(groups)
        assert info.ps_name == "ABC"

    def test_full_eight_chars(self):
        groups = _validated_ps_groups(pi=0x4321, ps_name="RadioZET")
        info = parse_groups(groups)
        assert info.ps_name == "RadioZET"

    def test_single_rotation_is_only_latest_candidate(self):
        groups = encode_ps_groups(pi=0x3001, ps_name="JEDYNKA ")
        info = parse_groups(groups)
        assert info.ps_name == ""
        assert info.latest_ps_candidate == "JEDYNKA "
        assert info._ps_stable_count == 1

    def test_static_ps_validates_after_two_rotations(self):
        groups = _validated_ps_groups(pi=0x3001, ps_name="JEDYNKA ")
        info = parse_groups(groups)
        assert info.ps_name == "JEDYNKA"
        assert info.validated_ps_name == "JEDYNKA"
        assert info.latest_ps_candidate == "JEDYNKA "

    def test_dynamic_ps_requires_consecutive_matching_completions(self):
        plus = encode_ps_groups(pi=0x3002, ps_name="+PLUS+  ")
        web = encode_ps_groups(pi=0x3002, ps_name="WWW.RPL ")
        info = parse_groups(plus + web + plus)
        assert info.ps_name == ""
        assert info.latest_ps_candidate == "+PLUS+  "
        assert info._ps_stable_count == 1

        for group in plus:
            parse_group(group, info)

        assert info.ps_name == "+PLUS+"
        assert info.latest_ps_candidate == "+PLUS+  "
        assert list(info._ps_history) == ["+PLUS+  ", "WWW.RPL ", "+PLUS+  ", "+PLUS+  "]

    def test_duplicate_segment_does_not_overwrite_candidate_rotation(self):
        groups = [
            encode_group_0a(pi=0x3003, ps_segment_index=0, ps_chars="JE"),
            encode_group_0a(pi=0x3003, ps_segment_index=0, ps_chars="XX"),
            encode_group_0a(pi=0x3003, ps_segment_index=1, ps_chars="DY"),
            encode_group_0a(pi=0x3003, ps_segment_index=2, ps_chars="NK"),
            encode_group_0a(pi=0x3003, ps_segment_index=3, ps_chars="A "),
        ]
        info = parse_groups(groups + groups)
        assert info.ps_name == "JEDYNKA"
        assert info.latest_ps_candidate == "JEDYNKA "
        assert info._ps_segments_seen == 0

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

    def test_rx_monotonic_timestamp_is_attached_to_clock_time(self):
        dt = datetime(2026, 5, 16, 14, 30, tzinfo=UTC)
        g = encode_group_4a(pi=0xCAFE, clock_time_local=dt)
        info = parse_groups([g], rx_monotonic_ns_by_group=[987_654_321])
        ct = info.latest_clock
        assert ct is not None
        assert ct.rx_monotonic_ns == 987_654_321

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
