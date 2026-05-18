"""Parser and encoder for RDS groups (PS, RT, Clock-Time).

Each group is 8 bytes = four 16-bit words (block A, B, C, D).

    Block A — PI code (Programme Identification)
    Block B — header (4-bit group type, 1-bit version, 1-bit TP,
                       5-bit PTY, 5 type-specific bits)
    Block C/D — payload depending on group type

Supported types:
    0A / 0B  — Programme Service name (PS), 8 chars in 4 segments
    2A / 2B  — RadioText (RT), up to 64 chars
    4A       — Clock-Time
"""

from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from .rds_blocks import group_bytes_to_words, group_words_to_bytes
from .rds_clock import ClockTime, decode_clock_time, encode_clock_time

PS_LEN = 8
RT_LEN = 64


@dataclass
class StationInfo:
    """Accumulator for fields decoded from many RDS groups of one station."""

    pi: int | None = None
    pty: int | None = None
    ps_chars: list[str] = field(default_factory=lambda: [" "] * PS_LEN)
    _ps_candidate: list[str] = field(default_factory=lambda: [" "] * PS_LEN)
    _ps_segments_seen: int = 0
    _ps_history: deque[str] = field(default_factory=lambda: deque(maxlen=4))
    _ps_stable_count: int = 0
    rt_chars: list[str] = field(default_factory=lambda: [" "] * RT_LEN)
    rt_ab_flag: int | None = None
    clock_times: list[ClockTime] = field(default_factory=list)
    group_counts: dict[str, int] = field(default_factory=dict)

    @property
    def ps_name(self) -> str:
        """Validated Programme Service name.

        Dynamic-PS stations often scroll text through the PS field. This value
        remains empty until the same complete 8-character frame has been seen
        in two consecutive PS completion cycles, then exposes the validated
        frame with the historical right-padding stripped.
        """
        return "".join(self.ps_chars).rstrip()

    @property
    def validated_ps_name(self) -> str:
        """Alias for :attr:`ps_name`, explicit about the validation semantics."""
        return self.ps_name

    @property
    def latest_ps_candidate(self) -> str:
        """Most recently received complete 8-character PS frame, without validation."""
        return self._ps_history[-1] if self._ps_history else ""

    @property
    def rt_text(self) -> str:
        text = "".join(self.rt_chars)
        # RadioText may terminate with 0x0D (carriage return) = end-of-text
        cr = text.find("\r")
        if cr >= 0:
            text = text[:cr]
        return text.rstrip()

    @property
    def latest_clock(self) -> ClockTime | None:
        return self.clock_times[-1] if self.clock_times else None


def _safe_char(byte: int) -> str:
    """Decode a single RDS character byte.

    RDS uses an extended character table; for an MVP decoder we accept
    printable ASCII 0x20..0x7E plus 0x0D (RadioText end marker).
    """
    if byte == 0x0D:
        return "\r"
    if 0x20 <= byte < 0x7F:
        return chr(byte)
    return " "


def parse_group(group_bytes: Sequence[int], info: StationInfo) -> StationInfo:
    """Interpret one RDS group and update ``info`` in place. Returns the same
    ``info`` to enable chaining."""
    a, b, c, d = group_bytes_to_words(group_bytes)

    info.pi = a if info.pi is None else info.pi  # first PI wins
    group_type = (b >> 12) & 0xF
    version = (b >> 11) & 0x1  # bit selects group version A or B
    info.pty = (b >> 5) & 0x1F if info.pty is None else info.pty

    label = f"{group_type}{'A' if version == 0 else 'B'}"
    info.group_counts[label] = info.group_counts.get(label, 0) + 1

    if group_type == 0:
        seg = b & 0x3  # 2-bit segment address (0..3)
        # Block D carries two PS characters
        bit = 1 << seg
        if not info._ps_segments_seen & bit:
            info._ps_candidate[seg * 2] = _safe_char((d >> 8) & 0xFF)
            info._ps_candidate[seg * 2 + 1] = _safe_char(d & 0xFF)
            info._ps_segments_seen |= bit
        if info._ps_segments_seen == 0b1111:
            candidate = "".join(info._ps_candidate)
            previous = info._ps_history[-1] if info._ps_history else None
            info._ps_history.append(candidate)
            if candidate == previous:
                info._ps_stable_count += 1
            else:
                info._ps_stable_count = 1
            if info._ps_stable_count >= 2:
                info.ps_chars[:] = list(candidate)
            info._ps_candidate = [" "] * PS_LEN
            info._ps_segments_seen = 0

    elif group_type == 2:
        ab_flag = (b >> 4) & 0x1
        if info.rt_ab_flag is not None and info.rt_ab_flag != ab_flag:
            info.rt_chars = [" "] * RT_LEN
        info.rt_ab_flag = ab_flag
        addr = b & 0xF
        if version == 0:
            # Group 2A: 4 RT chars in C + D
            info.rt_chars[addr * 4] = _safe_char((c >> 8) & 0xFF)
            info.rt_chars[addr * 4 + 1] = _safe_char(c & 0xFF)
            info.rt_chars[addr * 4 + 2] = _safe_char((d >> 8) & 0xFF)
            info.rt_chars[addr * 4 + 3] = _safe_char(d & 0xFF)
        else:
            # Group 2B: 2 RT chars in D
            info.rt_chars[addr * 2] = _safe_char((d >> 8) & 0xFF)
            info.rt_chars[addr * 2 + 1] = _safe_char(d & 0xFF)

    elif group_type == 4 and version == 0:
        # The two MSBs of MJD live in block B[1:0] — pass the FULL block B.
        ct = decode_clock_time(b, c, d)
        if ct is not None:
            info.clock_times.append(ct)

    return info


def parse_groups(groups: Iterable[Sequence[int]]) -> StationInfo:
    """Parse a sequence of groups, returning the accumulated ``StationInfo``."""
    info = StationInfo()
    for g in groups:
        parse_group(g, info)
    return info


# ----------------------- ENCODERS (for synthetic IQ) -----------------------


def encode_group_4a(
    pi: int, clock_time_local: datetime, pty: int = 0, tp: bool = False
) -> bytearray:
    """Return an 8-byte Group 4A (Clock-Time) for the given PI and local time.

    Block B layout (16 bits): ``[15:12]=0100``, ``[11]=0`` (version A),
    ``[10]=TP``, ``[9:5]=PTY``, ``[4:2]=spare (0)``, ``[1:0]=MJD[16:15]``.
    """
    block_b_extra, c, d = encode_clock_time(clock_time_local)
    # Only bits [1:0] of block_b_extra are meaningful (MJD high); rest is spare.
    b = (
        ((4 & 0xF) << 12)
        | (0 << 11)  # version A
        | ((1 if tp else 0) << 10)  # TP
        | ((pty & 0x1F) << 5)
        | (0 << 2)  # 3 spare bits = 0
        | (block_b_extra & 0x3)  # MJD[16:15]
    )
    return group_words_to_bytes((pi & 0xFFFF, b & 0xFFFF, c & 0xFFFF, d & 0xFFFF))


def encode_group_0a(
    pi: int,
    ps_segment_index: int,
    ps_chars: str,
    pty: int = 0,
    ta: bool = False,
    ms: bool = True,
    di_bit: int = 0,
    af1: int = 0,
    af2: int = 0,
) -> bytearray:
    """Return an 8-byte Group 0A with a single PS segment (two characters).

    Args:
        ps_segment_index: 0..3
        ps_chars: exactly two ASCII characters.
    """
    if not 0 <= ps_segment_index < 4:
        raise ValueError("ps_segment_index must be 0..3")
    if len(ps_chars) != 2:
        raise ValueError("ps_chars must be exactly 2 characters")

    b = (
        (0 << 12)
        | (0 << 11)  # version A
        | ((1 if ta else 0) << 10)
        | ((pty & 0x1F) << 5)
        | ((1 if ta else 0) << 4)
        | ((1 if ms else 0) << 3)
        | ((di_bit & 0x1) << 2)
        | (ps_segment_index & 0x3)
    )
    c = ((af1 & 0xFF) << 8) | (af2 & 0xFF)
    d = (ord(ps_chars[0]) << 8) | ord(ps_chars[1])
    return group_words_to_bytes((pi & 0xFFFF, b & 0xFFFF, c & 0xFFFF, d & 0xFFFF))


def encode_ps_groups(pi: int, ps_name: str, pty: int = 0) -> list[bytearray]:
    """Encode a full PS name (up to 8 chars) as four Group 0A frames
    (one per segment)."""
    name = ps_name.ljust(PS_LEN)[:PS_LEN]
    return [encode_group_0a(pi, seg, name[seg * 2 : seg * 2 + 2], pty=pty) for seg in range(4)]
