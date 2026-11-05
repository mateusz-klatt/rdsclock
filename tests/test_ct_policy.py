"""--ct-timezone: reading a station's CT "UTC" field as local time of an IANA zone."""

import re
from datetime import UTC, datetime

import pytest

from rdsclock.ct_policy import (
    CTInterpretation,
    CTPolicy,
    CTRule,
    interpret_in_zone,
    parse_ct_timezone,
)
from rdsclock.rds_clock import ClockTime, decode_clock_time

WARSAW = "Europe/Warsaw"


def _ct(*fields: int, lto: int = 0) -> ClockTime:
    """A decoded CT whose UTC field carries ``fields`` (year, month, day, hour, minute)."""
    return ClockTime(utc=datetime(*fields, tzinfo=UTC), local_offset_minutes=lto)


class TestParse:
    @pytest.mark.parametrize(
        "spec,rule",
        [
            ("Europe/Warsaw", CTRule("all", None, WARSAW)),
            ("UTC", CTRule("all", None, "UTC")),
            ("utc", CTRule("all", None, "UTC")),
            ("pi:0x3211=Europe/Warsaw", CTRule("pi", 0x3211, WARSAW)),
            ("pi:3211=Europe/Warsaw", CTRule("pi", 0x3211, WARSAW)),
            ("PI:0xa101=UTC", CTRule("pi", 0xA101, "UTC")),
            ("freq:98.8=Europe/Warsaw", CTRule("freq", 98.8, WARSAW)),
            (" freq: 107.5 = Europe/Warsaw ", CTRule("freq", 107.5, WARSAW)),
        ],
    )
    def test_valid_specs(self, spec, rule):
        assert parse_ct_timezone(spec) == rule

    @pytest.mark.parametrize(
        "spec,message",
        [
            ("", "empty time zone"),
            ("Europe/Warsw", "unknown time zone 'Europe/Warsw'"),
            ("Europe", "unknown time zone"),
            ("/etc/localtime", "unknown time zone"),
            ("../Europe/Warsaw", "unknown time zone"),
            ("pi:0x3211", "needs '=ZONE'"),
            ("pi:0x3211=", "missing time zone"),
            ("pi:zz=Europe/Warsaw", "PI must be a hex code"),
            ("pi:0x10000=Europe/Warsaw", "within 0x0000-0xFFFF"),
            ("pi:-1=Europe/Warsaw", "within 0x0000-0xFFFF"),
            ("freq:abc=Europe/Warsaw", "frequency must be a number"),
            ("freq:-98.8=Europe/Warsaw", "positive number"),
            ("freq:nan=Europe/Warsaw", "positive number"),
            ("psn:RMF=Europe/Warsaw", "selector must be pi:HEX or freq:MHZ"),
            ("=Europe/Warsaw", "selector must be"),
            ("freq:98.8=Mars/Olympus", "unknown time zone"),
        ],
    )
    def test_unusable_specs_are_explained(self, spec, message):
        with pytest.raises(ValueError, match=re.escape(message)):
            parse_ct_timezone(spec)

    @pytest.mark.parametrize(
        "spec", ["Europe/Warsaw", "UTC", "pi:0x3211=Europe/Warsaw", "freq:98.8=Europe/Warsaw"]
    )
    def test_a_rule_renders_back_to_its_spec(self, spec):
        rule = parse_ct_timezone(spec)
        assert str(rule) == spec
        assert parse_ct_timezone(str(rule)) == rule


class TestPolicy:
    def test_no_rules_reads_every_station_as_standard(self):
        assert CTPolicy().zone_for(0x3211, 102.4e6) == "UTC"
        assert CTPolicy().zone_for(None, None) == "UTC"

    def test_pi_beats_frequency_beats_every_station(self):
        policy = CTPolicy.from_specs(
            ["Europe/London", "freq:98.8=Europe/Berlin", "pi:0x3233=Europe/Warsaw"]
        )
        assert policy.zone_for(0x3233, 98.8e6) == WARSAW
        assert policy.zone_for(0x3233, 102.4e6) == WARSAW
        assert policy.zone_for(0x1234, 98.8e6) == "Europe/Berlin"
        assert policy.zone_for(None, 98.8e6) == "Europe/Berlin"
        assert policy.zone_for(0x1234, 102.4e6) == "Europe/London"
        assert policy.zone_for(None, None) == "Europe/London"

    def test_a_pi_rule_for_utc_exempts_one_station(self):
        policy = CTPolicy.from_specs(["Europe/Warsaw", "pi:0x3245=UTC"])
        assert policy.zone_for(0x3245, 98.3e6) == "UTC"
        assert policy.zone_for(0x3211, 102.4e6) == WARSAW

    def test_frequencies_match_by_100_khz_channel(self):
        policy = CTPolicy.from_specs(["freq:98.8=Europe/Warsaw"])
        assert policy.zone_for(None, 98.82e6) == WARSAW
        assert policy.zone_for(None, 98.78e6) == WARSAW
        assert policy.zone_for(None, 98.9e6) == "UTC"
        assert policy.zone_for(None, None) == "UTC"

    @pytest.mark.parametrize(
        "specs",
        [
            ["pi:0x3211=Europe/Warsaw", "pi:3211=UTC"],
            ["freq:98.8=Europe/Warsaw", "freq:98.80=Europe/Berlin"],
            ["freq:98.8=Europe/Warsaw", "freq:98.84=Europe/Berlin"],
            ["Europe/Warsaw", "Europe/Berlin"],
        ],
    )
    def test_same_selector_with_different_zones_is_refused(self, specs):
        with pytest.raises(ValueError, match="conflicting --ct-timezone rules"):
            CTPolicy.from_specs(specs)

    def test_repeating_a_rule_is_harmless(self):
        policy = CTPolicy.from_specs(["pi:0x3211=Europe/Warsaw", "pi:0x3211=Europe/Warsaw"])
        assert policy.rules == (CTRule("pi", 0x3211, WARSAW),)
        assert policy == CTPolicy.from_specs(["pi:0x3211=Europe/Warsaw"])

    def test_interpret_uses_the_matching_zone(self):
        policy = CTPolicy.from_specs(["pi:0x3211=Europe/Warsaw"])
        ct = _ct(2026, 11, 3, 10, 6)
        assert policy.interpret(ct, 0x3211, 102.4e6).status == "converted"
        assert policy.interpret(ct, 0x3233, 98.8e6) == CTInterpretation(ct.utc, "UTC", "standard")


class TestInterpretation:
    def test_standard_zone_leaves_the_ct_unchanged(self):
        ct = _ct(2026, 7, 1, 12, 0, lto=120)
        assert interpret_in_zone(ct, "UTC") == CTInterpretation(ct.utc, "UTC", "standard")

    def test_measured_jedynka_group_converts_to_real_utc(self):
        # Raw group 3211 4461 DF40 A180 (MJD 61344) received at 09:06 real UTC in Warsaw.
        ct = decode_clock_time(0x4461, 0xDF40, 0xA180)
        assert ct is not None
        assert (ct.utc, ct.local_offset_minutes) == (datetime(2026, 10, 31, 10, 6, tzinfo=UTC), 0)
        result = interpret_in_zone(ct, WARSAW)
        assert result == CTInterpretation(
            datetime(2026, 10, 31, 9, 6, tzinfo=UTC), WARSAW, "converted"
        )

    def test_winter_after_the_october_change_is_utc_plus_one(self):
        result = interpret_in_zone(_ct(2026, 10, 31, 10, 6), WARSAW)
        assert result.utc == datetime(2026, 10, 31, 9, 6, tzinfo=UTC)
        assert result.status == "converted"

    def test_summer_is_utc_plus_two(self):
        result = interpret_in_zone(_ct(2026, 7, 1, 12, 0), WARSAW)
        assert result.utc == datetime(2026, 7, 1, 10, 0, tzinfo=UTC)

    def test_the_lto_is_kept_but_not_applied(self):
        ct = _ct(2026, 7, 1, 12, 0, lto=120)
        assert interpret_in_zone(ct, WARSAW).utc == datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
        assert ct.local_offset_minutes == 120

    def test_the_repeated_fall_back_hour_is_ambiguous(self):
        result = interpret_in_zone(_ct(2026, 10, 25, 2, 30), WARSAW)
        assert result == CTInterpretation(None, WARSAW, "ambiguous")

    def test_the_hours_around_fall_back_are_unambiguous(self):
        before = interpret_in_zone(_ct(2026, 10, 25, 1, 59), WARSAW)
        after = interpret_in_zone(_ct(2026, 10, 25, 3, 0), WARSAW)
        assert before.utc == datetime(2026, 10, 24, 23, 59, tzinfo=UTC)
        assert after.utc == datetime(2026, 10, 25, 2, 0, tzinfo=UTC)

    def test_the_spring_forward_gap_does_not_exist(self):
        result = interpret_in_zone(_ct(2026, 3, 29, 2, 30), WARSAW)
        assert result == CTInterpretation(None, WARSAW, "nonexistent")

    def test_the_hours_around_spring_forward_convert(self):
        before = interpret_in_zone(_ct(2026, 3, 29, 1, 59), WARSAW)
        after = interpret_in_zone(_ct(2026, 3, 29, 3, 0), WARSAW)
        assert before.utc == datetime(2026, 3, 29, 0, 59, tzinfo=UTC)
        assert after.utc == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        "local,utc",
        [
            ((2026, 11, 1, 0, 30), (2026, 10, 31, 23, 30)),
            ((2026, 7, 1, 1, 0), (2026, 6, 30, 23, 0)),
            ((2027, 1, 1, 0, 0), (2026, 12, 31, 23, 0)),
        ],
    )
    def test_conversion_rolls_the_date_back_over_midnight(self, local, utc):
        assert interpret_in_zone(_ct(*local), WARSAW).utc == datetime(*utc, tzinfo=UTC)

    def test_a_zone_west_of_utc_rolls_the_date_forward(self):
        result = interpret_in_zone(_ct(2026, 11, 3, 22, 0), "America/New_York")
        assert result.utc == datetime(2026, 11, 4, 3, 0, tzinfo=UTC)


def test_frequency_rules_round_trip_to_the_channel_they_select():
    from rdsclock.ct_policy import parse_ct_timezone

    rule = parse_ct_timezone("freq:98.85001=Europe/Warsaw")
    again = parse_ct_timezone(str(rule))
    assert str(rule) == "freq:98.9=Europe/Warsaw"
    assert again.selector == rule.selector
    assert again.matches(None, 98.9e6) and not again.matches(None, 98.8e6)


def test_absurd_frequencies_are_rejected_when_parsing():
    import pytest

    from rdsclock.ct_policy import parse_ct_timezone

    for spec in ("freq:1e308=UTC", "freq:10000=UTC"):
        with pytest.raises(ValueError, match="below 10000"):
            parse_ct_timezone(spec)
