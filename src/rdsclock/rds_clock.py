"""RDS Clock-Time (Group 4A): MJD ↔ datetime, encode + decode.

Layout of Group 4A as defined by IEC 62106-2:2021, Figure 11
("Clock-time and date — Group type 4A"). See also the public summary
at https://en.wikipedia.org/wiki/Radio_Data_System.

    Block B (16-bit header):
        bits 15..12 : group type code = 0100 (= 4)
        bit  11     : B0 / version  (0 = A)
        bit  10     : TP  (Traffic Program)
        bits  9..5  : PTY (program type)
        bits  4..2  : spare (zero)
        bits  1..0  : MJD[16..15]   ← two most significant bits of MJD

    Block C (16 bits):
        bits 15..1  : MJD[14..0]     (15 lower bits of MJD)
        bit   0     : HOUR[4]        (MSB of hour)

    Block D (16 bits):
        bits 15..12 : HOUR[3..0]     (4 LSBs of hour)
        bits 11..6  : MINUTE[5..0]
        bit   5     : LTO sign (1 = negative / west, 0 = positive)
        bits  4..0  : LTO magnitude in units of 30 minutes

Field width sum: 17 + 5 + 6 + 1 + 5 = 34 bits, distributed as
2 bits in B, 16 bits in C, 16 bits in D.

Equivalent bit-extraction formulas:
    MJD    = ((B & 0x0003) << 15) | (C >> 1)
    HOUR   = ((C & 0x1) << 4) | (D >> 12)
    MINUTE = (D >> 6) & 0x3F
    SIGN   = (D >> 5) & 0x1
    MAG    = D & 0x1F

MJD → datetime: epoch is 1858-11-17 (Reduced Julian Day epoch);
``date = epoch + days(MJD)``. The hh:mm payload is UTC; local time
= UTC + sign · magnitude · 30 min.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

# MJD epoch: 17 November 1858, 00:00 UT.
MJD_EPOCH = datetime(1858, 11, 17, 0, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class ClockTime:
    """Decoded RDS Group 4A clock: UTC moment plus local time offset."""

    utc: datetime  # always has tzinfo=UTC
    local_offset_minutes: int  # may be negative
    rx_monotonic_ns: int | None = None
    tx_latency_ns: int | None = None

    @property
    def local(self) -> datetime:
        """Local time using a timezone constructed from the LTO field."""
        tz = timezone(timedelta(minutes=self.local_offset_minutes))
        return self.utc.astimezone(tz)

    def __str__(self) -> str:
        sign = "+" if self.local_offset_minutes >= 0 else "-"
        mag = abs(self.local_offset_minutes)
        return f"{self.utc.strftime('%Y-%m-%d %H:%M')}Z (LTO {sign}{mag // 60:02d}:{mag % 60:02d})"


def datetime_to_mjd(dt: datetime) -> int:
    """Return the Modified Julian Day (integer) for a UTC datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt.astimezone(UTC) - MJD_EPOCH
    return delta.days


def mjd_to_date(mjd: int) -> datetime:
    """Return a UTC datetime at 00:00 for the given MJD."""
    return MJD_EPOCH + timedelta(days=int(mjd))


def encode_clock_time(
    local_dt: datetime, local_offset_minutes: int | None = None
) -> tuple[int, int, int]:
    """Build the (block_b_extra, block_c, block_d) payload triple for Group 4A.

    ``block_b_extra`` carries only block B bits [4:0] specific to the
    Clock-Time payload: bits [4:2] are the spare zero, bits [1:0] are
    MJD[16:15]. The rest of block B (group type, version, TP, PTY) is
    assembled by :func:`rdsclock.rds_groups.encode_group_4a`.

    Args:
        local_dt: local timestamp (tz-aware). A naive datetime is
            interpreted as UTC.
        local_offset_minutes: overrides the LTO. If ``None``, the
            offset is taken from ``local_dt.tzinfo``.

    Returns:
        ``(block_b_extra, block_c, block_d)`` — three 16-bit integers,
        but only bits [4:0] of ``block_b_extra`` are significant.
    """
    if local_dt.tzinfo is None:
        if local_offset_minutes is None:
            local_offset_minutes = 0
        local_dt = local_dt.replace(tzinfo=timezone(timedelta(minutes=local_offset_minutes)))
    elif local_offset_minutes is None:
        utc_off = local_dt.utcoffset()
        local_offset_minutes = 0 if utc_off is None else int(utc_off.total_seconds() // 60)

    utc_dt = local_dt.astimezone(UTC)
    mjd = datetime_to_mjd(utc_dt)
    if not 0 <= mjd < (1 << 17):
        raise ValueError(f"MJD out of 17-bit range: {mjd}")

    hour = utc_dt.hour
    minute = utc_dt.minute
    if not (0 <= hour < 24 and 0 <= minute < 60):  # pragma: no cover - datetime guarantees this
        raise ValueError(f"hour/minute out of range: {hour}:{minute}")

    if local_offset_minutes % 30 != 0:
        raise ValueError(
            f"local_offset_minutes must be a multiple of 30, got {local_offset_minutes}"
        )
    sign = 1 if local_offset_minutes < 0 else 0
    magnitude = abs(local_offset_minutes) // 30
    if magnitude >= (1 << 5):
        raise ValueError(f"LTO out of ±15.5 h (magnitude {magnitude} half-hours)")

    # Split MJD: top 2 bits → block B[1:0], remaining 15 → block C[15:1]
    mjd_high = (mjd >> 15) & 0x3  # MJD[16..15]
    mjd_low15 = mjd & 0x7FFF  # MJD[14..0]

    # Block B extra payload: bits 4..0; bits 1..0 = mjd_high, bits 4..2 spare=0
    block_b_extra = mjd_high & 0x3

    # Block C: bits 15..1 = MJD[14..0], bit 0 = HOUR[4]
    block_c = ((mjd_low15 & 0x7FFF) << 1) | ((hour >> 4) & 0x1)

    # Block D: bits 15..12 = HOUR[3..0], 11..6 = MINUTE, 5 = sign, 4..0 = magnitude
    block_d = (
        ((hour & 0xF) << 12) | ((minute & 0x3F) << 6) | ((sign & 0x1) << 5) | (magnitude & 0x1F)
    )

    return block_b_extra & 0x1F, block_c & 0xFFFF, block_d & 0xFFFF


def decode_clock_time(
    block_b: int,
    block_c: int,
    block_d: int,
    rx_monotonic_ns: int | None = None,
) -> ClockTime | None:
    """Decode Group 4A. Returns ``None`` if the fields fall outside
    sensible ranges (typical for stations transmitting dummy CT data).

    The full 16-bit block B value is required because the two most
    significant MJD bits live in ``B[1:0]``.
    """
    if not all(0 <= v < (1 << 16) for v in (block_b, block_c, block_d)):
        return None

    mjd = ((block_b & 0x3) << 15) | ((block_c >> 1) & 0x7FFF)
    hour = ((block_c & 0x1) << 4) | ((block_d >> 12) & 0xF)
    minute = (block_d >> 6) & 0x3F
    sign_bit = (block_d >> 5) & 0x1
    magnitude = block_d & 0x1F

    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    # Sensible MJD range: 1900-01-01 (15020) to 2100-12-31 (~88069)
    if not 15020 <= mjd <= 88069:
        return None

    try:
        base_date = mjd_to_date(mjd)
        utc_dt = base_date.replace(hour=hour, minute=minute)
    except Exception:  # pragma: no cover - validated fields make datetime.replace() safe here
        return None

    offset_minutes = magnitude * 30
    if sign_bit:
        offset_minutes = -offset_minutes
    return ClockTime(
        utc=utc_dt,
        local_offset_minutes=offset_minutes,
        rx_monotonic_ns=rx_monotonic_ns,
    )
