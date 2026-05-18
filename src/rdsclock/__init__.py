"""rdsclock — passive RDS Clock-Time receiver for FM via RTL-SDR.

Package focused on RDS (Radio Data System) decoding, with special
emphasis on Group 4A (Clock-Time) — recovering UTC from FM broadcast
in GPS-denied or NTP-unavailable environments.

Modules:
    dsp              DSP primitives: filters, FM demod, Costas, clock recovery.
    rds_blocks       Block-level CRC, syndromes, bitstream → byte groups.
    rds_clock        MJD ↔ datetime, Group 4A encode/decode (IEC 62106-2:2021).
    rds_groups       PS / RT / Clock-Time parser and encoder.
    synth            Synthetic IQ generator: bits → BPSK 57 kHz → MPX → FM IQ + AWGN.
    decoder          End-to-end IQ → groups → StationInfo pipeline.
    channelizer      Wide-band capture → N narrow per-station channels.
    rtl_tcp          Minimal rtl_tcp client (context manager).
    time_consensus   Multi-source time consensus, trust scoring, holdover.
    recon            Continuous passive time receiver (live + offline modes).
    cli              Command-line interface: generate / decode / live / multi /
                     scan / recon / demo.
"""

from .decoder import DecodeResult, decode_file, decode_iq
from .rds_clock import ClockTime
from .rds_groups import StationInfo
from .time_consensus import SubSecondEstimate, TimeConsensus

__version__ = "1.0.1"

__all__ = [
    "__version__",
    "decode_file",
    "decode_iq",
    "DecodeResult",
    "ClockTime",
    "StationInfo",
    "TimeConsensus",
    "SubSecondEstimate",
]
