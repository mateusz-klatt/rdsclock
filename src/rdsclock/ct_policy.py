"""Per-station interpretation of the RDS Clock-Time "UTC" field.

IEC 62106 defines the Group 4A hour and minute as UTC, with local time
= UTC + LTO. Some broadcasters violate this and transmit their local civil
time in the UTC field instead, usually with LTO 0 (for example several
Polish stations send CET/CEST there). Taken at face value such stations sit
one or two hours away from honest ones, depending on the season.

The operator can declare this per station with an IANA zone: the decoded
hour and minute are then read as wall-clock time in that zone and converted
to real UTC, so daylight saving changes are followed without manual offsets.
Nothing is corrected automatically; the default treats every station as
standard.

Rule syntax (see :func:`parse_ct_timezone`)::

    ZONE                  every station
    pi:0x3211=ZONE        stations with this PI code (hex)
    freq:98.8=ZONE        stations on this frequency (MHz, 100 kHz channel)

``ZONE`` is ``UTC`` (the standard behaviour) or an IANA name such as
``Europe/Warsaw``. A PI rule wins over a frequency rule, which wins over a
rule for every station.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .rds_clock import ClockTime

STANDARD_ZONE = "UTC"

CTStatus = Literal["standard", "converted", "ambiguous", "nonexistent"]
RuleKind = Literal["all", "pi", "freq"]

# Higher wins: a PI names one programme, a frequency one channel.
_PRECEDENCE: dict[str, int] = {"all": 0, "freq": 1, "pi": 2}


def _channel(freq_hz: float) -> int:
    """The 100 kHz channel of a tuning, as used for consensus tracks."""
    return round(freq_hz / 100_000)


def _validate_zone(zone: str) -> str:
    if zone.upper() == STANDARD_ZONE:
        return STANDARD_ZONE
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"unknown time zone {zone!r} (use UTC or an IANA name)") from exc
    return zone


@dataclass(frozen=True)
class CTRule:
    """One ``--ct-timezone`` rule: which stations it covers and their zone."""

    kind: RuleKind
    value: int | float | None
    zone: str

    @property
    def precedence(self) -> int:
        return _PRECEDENCE[self.kind]

    @property
    def selector(self) -> tuple[str, int | None]:
        """Identity of the covered stations; equal selectors cover the same ones."""
        if self.kind == "pi" and self.value is not None:
            return ("pi", int(self.value))
        if self.kind == "freq" and self.value is not None:
            return ("freq", _channel(self.value * 1e6))
        return ("all", None)

    def matches(self, pi: int | None, freq_hz: float | None) -> bool:
        if self.kind == "all":
            return True
        if self.kind == "pi":
            return pi is not None and pi == self.value
        return freq_hz is not None and _channel(freq_hz) == self.selector[1]

    def __str__(self) -> str:
        """The rule as a ``--ct-timezone`` argument that parses back to it."""
        if self.kind == "pi":
            return f"pi:0x{self.value:04X}={self.zone}"
        if self.kind == "freq":
            return f"freq:{self.value:g}={self.zone}"
        return self.zone


def parse_ct_timezone(spec: str) -> CTRule:
    """Parse ``ZONE``, ``pi:0xPPPP=ZONE`` or ``freq:MHZ=ZONE``; ``ValueError`` if unusable."""
    text = spec.strip()
    if "=" not in text:
        if ":" in text.split("/", 1)[0]:
            raise ValueError(f"{spec!r}: a selector needs '=ZONE', e.g. pi:0x3211=Europe/Warsaw")
        if not text:
            raise ValueError("empty time zone")
        return CTRule("all", None, _validate_zone(text))

    selector, zone = (part.strip() for part in text.split("=", 1))
    kind, sep, raw = (part.strip() for part in selector.partition(":"))
    if not sep or kind.lower() not in ("pi", "freq"):
        raise ValueError(f"{spec!r}: selector must be pi:HEX or freq:MHZ")
    if not zone:
        raise ValueError(f"{spec!r}: missing time zone after '='")
    if kind.lower() == "pi":
        try:
            # PI codes are always written in hex; accept them with or without 0x.
            pi = int(raw, 16)
        except ValueError as exc:
            raise ValueError(f"{spec!r}: PI must be a hex code such as 0x3211") from exc
        if not 0 <= pi <= 0xFFFF:
            raise ValueError(f"{spec!r}: PI must be within 0x0000-0xFFFF")
        return CTRule("pi", pi, _validate_zone(zone))
    try:
        mhz = float(raw)
    except ValueError as exc:
        raise ValueError(f"{spec!r}: frequency must be a number in MHz such as 98.8") from exc
    if not (math.isfinite(mhz) and mhz > 0):
        raise ValueError(f"{spec!r}: frequency must be a positive number in MHz")
    return CTRule("freq", mhz, _validate_zone(zone))


@dataclass(frozen=True)
class CTInterpretation:
    """How one decoded Clock-Time was read.

    ``utc`` is the effective UTC that may enter the consensus, or ``None``
    when the local wall-clock time cannot be mapped to one instant.
    """

    utc: datetime | None
    zone: str
    status: CTStatus


def interpret_in_zone(ct: ClockTime, zone: str) -> CTInterpretation:
    """Read ``ct`` as standard UTC, or as wall-clock time in an IANA ``zone``.

    In converted mode the transmitted LTO is kept as is but not applied:
    the stations this exists for send LTO 0 next to local time, so adding
    it would change nothing at best and double-count at worst.
    """
    if zone == STANDARD_ZONE:
        return CTInterpretation(ct.utc, STANDARD_ZONE, "standard")
    tz = ZoneInfo(zone)
    wall = ct.utc.replace(tzinfo=None)
    earlier = wall.replace(tzinfo=tz, fold=0)
    later = wall.replace(tzinfo=tz, fold=1)
    # A time skipped by a spring-forward gap does not survive the round trip.
    if earlier.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != wall:
        return CTInterpretation(None, zone, "nonexistent")
    # A repeated fall-back hour maps to two instants; refuse to guess.
    if earlier.utcoffset() != later.utcoffset():
        return CTInterpretation(None, zone, "ambiguous")
    return CTInterpretation(earlier.astimezone(UTC), zone, "converted")


@dataclass(frozen=True)
class CTPolicy:
    """The ``--ct-timezone`` rules of one receiver; no rules means all standard.

    Two rules with the same selector but different zones are rejected with
    ``ValueError``: they would have the same precedence for the same station.
    """

    rules: tuple[CTRule, ...] = ()

    def __post_init__(self) -> None:
        unique: dict[tuple[str, int | None], CTRule] = {}
        for rule in self.rules:
            previous = unique.get(rule.selector)
            if previous is not None and previous.zone != rule.zone:
                raise ValueError(
                    f"conflicting --ct-timezone rules for the same stations: "
                    f"{previous} and {rule}"
                )
            unique.setdefault(rule.selector, rule)
        object.__setattr__(self, "rules", tuple(unique.values()))

    @classmethod
    def from_specs(cls, specs: Iterable[str]) -> "CTPolicy":
        return cls(tuple(parse_ct_timezone(spec) for spec in specs))

    def zone_for(self, pi: int | None, freq_hz: float | None) -> str:
        """The zone of the most specific matching rule, or UTC."""
        best: CTRule | None = None
        for rule in self.rules:
            if rule.matches(pi, freq_hz) and (best is None or rule.precedence > best.precedence):
                best = rule
        return STANDARD_ZONE if best is None else best.zone

    def interpret(self, ct: ClockTime, pi: int | None, freq_hz: float | None) -> CTInterpretation:
        return interpret_in_zone(ct, self.zone_for(pi, freq_hz))
