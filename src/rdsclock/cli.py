"""Command-line interface: ``python -m rdsclock <command> ...``.

Sub-commands:
    generate   — synthesise an IQ file with an embedded clock (for tests/demo)
    decode     — decode the clock from an IQ file
    live       — connect to rtl_tcp, record N seconds, decode
    scan       — sweep the FM band, attempt to decode each station
    multi      — multi-station decode (auto WIDE if in range, otherwise HOP)
    demo       — self-contained 3-station multi-channel demo (no SDR needed)
    recon      — continuous passive time receiver (live or offline mode; HOP or WIDE)
"""

import argparse
import contextlib
import importlib.util
import math
import os
import signal
import sys
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from math import gcd

import numpy as np
from scipy.signal import resample_poly

from . import dsp
from .audio import DEFAULT_AUDIO_RATE, DEFAULT_LIVE_FS, play_iq_file, play_iq_live
from .channelizer import (
    ChannelDecodeResult,
    ChannelSpec,
    auto_center,
    decode_channels,
    fits_in_window,
    outside_capture,
    required_bandwidth,
)
from .ct_policy import CTPolicy, CTRule, parse_ct_timezone
from .decoder import decode_file, decode_iq
from .i18n import SUPPORTED_LANGUAGES, translate
from .plot import plot_iq_waterfall, plot_mpx_spectrum
from .rds_groups import StationInfo, encode_group_4a, encode_ps_groups
from .recon import (
    WIDE_SAMPLE_RATE,
    ReconConfig,
    ReconEvent,
    event_to_json,
    run_recon,
    run_recon_offline,
)
from .rtl_tcp import RtlTcpAddress, RtlTcpClient
from .synth import DEFAULT_FS, synthesize_fm_iq

# ---------- helpers ----------

_GAIN_AGC_LINE = "  Gain: AGC"


def _configure_receiver(
    client: RtlTcpClient, sample_rate: int, gain_db: float | None, ppm: int = 0
) -> None:
    """Start each FM session from explicit receiver settings."""
    client.configure_fm(sample_rate, gain_db=gain_db, ppm=ppm)
    if gain_db is None:
        print(_GAIN_AGC_LINE)
    else:
        print(f"  Gain: manual {gain_db} dB")


def _full_scale_fraction(iq: np.ndarray) -> float:
    """Fraction of raw I/Q components at the uint8 converter's end codes."""
    if not len(iq):
        return 0.0
    return float(
        (np.count_nonzero(np.abs(iq.real) >= 1) + np.count_nonzero(np.abs(iq.imag) >= 1))
        / (2 * len(iq))
    )


def _print_capture_quality(iq: np.ndarray) -> None:
    rails = _full_scale_fraction(iq)
    print(f"  Full-scale I/Q: {rails:.2%}")
    if rails > 0.01:
        print("  Many I/Q values reach full scale; compare a lower manual --gain.")


def _scan_mark(decoded) -> str:
    """Single-character label for a scan-line: CT, FM-only or silent."""
    if decoded.info.latest_clock is not None:
        return "[CT]"
    if decoded.n_groups > 0:
        return "[FM]"
    return "[--]"


def _print_station(info: StationInfo) -> None:
    pi = f"0x{info.pi:04X}" if info.pi is not None else "—"
    pty = info.pty if info.pty is not None else "—"
    print(f"  PI:        {pi}")
    print(f"  PTY:       {pty}")
    print(f"  PS:        '{info.ps_name}'")
    print(f"  RT:        '{info.rt_text}'")
    if info.clock_times:
        ct = info.latest_clock
        if ct is not None:
            print(f"  ClockTime: {ct}  → local: {ct.local.strftime('%Y-%m-%d %H:%M %Z')}")
            if ct.rx_monotonic_ns is not None:
                print(f"  rx_monotonic: {ct.rx_monotonic_ns:_d} ns (host time at receipt)")
            print(f"  CT count:  {len(info.clock_times)}")
    else:
        print("  ClockTime: NONE (no valid Group 4A in this capture; try 70–130 seconds)")
    if info.group_counts:
        items = sorted(info.group_counts.items(), key=lambda kv: -kv[1])
        print(f"  Groups:    {dict(items)}")


def _capture_start_iso_to_ns(value: str | None) -> int | None:
    if value is None:
        return None
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1_000_000_000)


def _trim_settle_iq(iq: np.ndarray, fs: int, settle_seconds: float) -> tuple[np.ndarray, int]:
    """Drop retune-settling samples when doing so leaves samples to decode."""
    n_settle = int(round(settle_seconds * fs))
    if n_settle <= 0 or n_settle >= len(iq):
        return iq, 0
    return iq[n_settle:], n_settle


# ---------- generate ----------


def cmd_generate(args: argparse.Namespace) -> int:
    """Synthesise an FM IQ file with the current time encoded as RDS Clock-Time."""
    ts = datetime.fromisoformat(args.time) if args.time else datetime.now()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)

    pi = int(args.pi, 0) if isinstance(args.pi, str) else args.pi

    # Build a small group set: 4× 0A (PS) + 1× 4A (CT), repeated as a carousel.
    ps_groups = encode_ps_groups(pi=pi, ps_name=args.ps, pty=args.pty)
    ct_group = encode_group_4a(pi=pi, clock_time_local=ts, pty=args.pty)
    groups = ps_groups + [ct_group]

    rng = np.random.default_rng(args.seed) if args.seed is not None else None
    iq = synthesize_fm_iq(
        groups=groups,
        duration_s=args.duration,
        fs=args.fs,
        snr_db=args.snr,
        carrier_offset_hz=args.offset,
        rng=rng,
        audio_tone_hz=args.audio_tone,
    )

    if args.format == "complex64":
        dsp.write_iq_complex64(iq, args.output)
    else:
        dsp.write_iq_u8(iq, args.output)

    size = os.path.getsize(args.output)
    print(f"Wrote: {args.output}  ({len(iq)} samples, {size:,} B)")
    print(f"Embedded UTC time: {ts.astimezone(UTC).isoformat()}")
    print(f"PI: 0x{pi:04X}  PS: '{args.ps}'  PTY: {args.pty}")
    print(
        f"fs: {args.fs} Hz, duration: {args.duration}s, SNR: {args.snr} dB, "
        f"carrier offset: {args.offset} Hz"
    )
    return 0


# ---------- decode ----------


def cmd_decode(args: argparse.Namespace) -> int:
    def progress(msg: str) -> None:
        if args.verbose:
            print(f"  ▸ {msg}")

    start = time.time()
    capture_start_ns = _capture_start_iso_to_ns(args.capture_start_iso)
    if args.carrier_hz is not None:
        try:
            iq = dsp.read_iq_complex64(args.file)
            if len(iq) == 0:
                iq = dsp.read_iq_u8(args.file)
        except Exception:
            iq = dsp.read_iq_u8(args.file)
        decode_kwargs = {
            "fs": args.fs,
            "carrier_hz": args.carrier_hz,
            "auto_carrier": False,
            "progress": progress,
        }
        if capture_start_ns is not None:
            decode_kwargs["capture_start_monotonic_ns"] = capture_start_ns
        result = decode_iq(iq, **decode_kwargs)
    else:
        if capture_start_ns is None:
            result = decode_file(args.file, fs=args.fs, progress=progress)
        else:
            result = decode_file(
                args.file,
                fs=args.fs,
                progress=progress,
                capture_start_monotonic_ns=capture_start_ns,
            )
    elapsed = time.time() - start

    print(f"\n=== {args.file} ===")
    print(f"  Bits:       {result.n_bits}")
    print(f"  Groups:     {result.n_groups}")
    print(f"  CRC:        {result.n_groups_clean} clean, {result.n_groups_corrected} corrected")
    print(f"  Sym off:    {result.symbol_offset}")
    print(f"  Time:       {elapsed:.1f}s")
    _print_station(result.info)
    return 0


# ---------- live ----------


def cmd_live(args: argparse.Namespace) -> int:
    print(f"Connecting to rtl_tcp {args.host}:{args.port}…")
    with RtlTcpClient(host=args.host, port=args.port) as client:
        info = client.info
        print(f"  Tuner: {info.tuner_type}, gain count: {info.gain_count}")
        _configure_receiver(client, args.fs, args.gain, args.ppm)
        if args.ppm:
            print(f"  PPM correction: {args.ppm}")
        client.set_frequency(int(args.freq * 1e6))

        print(f"Recording {args.duration}s @ {args.freq} MHz …")
        capture = client.capture_fresh(args.duration, args.fs, settle_s=args.settle_seconds)
        iq = capture.iq
        print(
            f"  samples: {len(iq)}  power: "
            f"{10 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-12):.1f} dBFS"
        )
        _print_capture_quality(iq)
        if args.verbose:
            print(f"  discarded before capture: {capture.discarded_samples} samples")
            print(f"  queued when the capture began: {capture.queued_s:.3f} s")
        if not capture.caught_up:
            print(
                f"  the capture began at least {capture.queued_s:.1f} s behind real time "
                "and never caught up: receive times are unknown"
            )

    if args.save:
        dsp.write_iq_complex64(iq, args.save)
        print(f"  saved: {args.save}")

    result = decode_iq(
        iq,
        fs=args.fs,
        carrier_hz=args.carrier_hz,
        auto_carrier=args.carrier_hz is None,
        progress=(lambda m: print(f"  ▸ {m}")) if args.verbose else None,
        capture_start_monotonic_ns=capture.capture_start_monotonic_ns,
    )
    print(
        f"\n  Groups: {result.n_groups} ({result.n_groups_clean} clean, "
        f"{result.n_groups_corrected} corrected)  Bits: {result.n_bits}"
    )
    _print_station(result.info)
    ct = result.info.latest_clock
    if ct is not None and ct.rx_monotonic_ns is not None and capture.caught_up:
        estimated_now = ct.utc + timedelta(seconds=(time.monotonic_ns() - ct.rx_monotonic_ns) / 1e9)
        difference = (estimated_now - datetime.now(UTC)).total_seconds()
        print(
            f"  CT versus host UTC: approximately {difference:+.1f}s (host is a comparison, not ground truth)"
        )
    return 0


# ---------- scan ----------


def _scan_one_frequency(client: RtlTcpClient, freq_mhz: float, args: argparse.Namespace) -> str:
    """Capture, decode and pretty-print a single scan line. Returns the line."""
    client.set_frequency(int(freq_mhz * 1e6))
    capture = client.capture_fresh(
        args.duration, args.fs, settle_s=getattr(args, "settle_seconds", 0.3)
    )
    iq = capture.iq
    power_db = 10 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-12)
    decoded = decode_iq(iq, fs=args.fs)
    mark = _scan_mark(decoded)
    has_ct = "YES" if decoded.info.latest_clock is not None else "—"
    line = (
        f"{mark} {freq_mhz:6.2f} MHz  {power_db:+6.1f} dBFS  "
        f"groups={decoded.n_groups:4d}  PI={f'{decoded.info.pi:04X}' if decoded.info.pi is not None else '----'}  "
        f"rails={_full_scale_fraction(iq):.1%}  PS='{decoded.info.ps_name:<8}'  "
        f"CT={has_ct}"
    )
    print(line)
    return line


def cmd_scan(args: argparse.Namespace) -> int:
    print(f"Scanning {args.start}-{args.end} MHz, step {args.step} MHz @ {args.fs} S/s…")
    with RtlTcpClient(host=args.host, port=args.port) as client:
        _configure_receiver(client, args.fs, args.gain)

        results: list[str] = []
        freq_mhz = args.start
        while freq_mhz <= args.end + 1e-6:
            line = _scan_one_frequency(client, freq_mhz, args)
            if "[CT]" in line:
                results.append(line)
            freq_mhz += args.step

    if results:
        print("\nStations with Clock-Time:")
        for line in results:
            print(line)
    else:
        print("\nNo valid Clock-Time observed during this scan; short captures may miss Group 4A.")
    return 0


# ---------- multi ----------


def _print_results_table(results) -> None:
    """Common results table (shared by wide and hop modes)."""
    print("=" * 84)
    print(f"{'STATION':<14} {'PS':<10} {'PI':<8} {'GROUPS':<7} {'CLOCK TIME (UTC)':<25} {'LTO'}")
    print("-" * 84)
    for r in results:
        ct = r.result.info.latest_clock
        ct_str = ct.utc.strftime("%Y-%m-%d %H:%M") if ct else "—"
        lto_str = f"{ct.local_offset_minutes:+d} min" if ct else "—"
        pi = f"0x{r.result.info.pi:04X}" if r.result.info.pi is not None else "—"
        print(
            f"{r.spec.label:<14} {r.result.info.ps_name:<10} {pi:<8} "
            f"{r.result.n_groups:<7} {ct_str:<25} {lto_str}"
        )
    print("=" * 84)


def _multi_wide(freqs_mhz: list[float], center_hz: float, args: argparse.Namespace) -> int:
    """WIDE mode: capture all stations in a single recording, channelize digitally."""
    min_bw = required_bandwidth([f * 1e6 for f in freqs_mhz])
    print("Multi-station decoder (WIDE mode)")
    print(f"  Stations:      {', '.join(f'{f:.2f} MHz' for f in freqs_mhz)}")
    print(f"  Center:        {center_hz / 1e6:.3f} MHz")
    print(f"  Wide fs:       {args.fs / 1e6:.3f} MS/s")
    print(f"  Required BW:   {min_bw / 1e6:.2f} MHz")
    print(f"  Duration:      {args.duration}s")
    print()

    print(f"Connecting to rtl_tcp {args.host}:{args.port}…")
    with RtlTcpClient(host=args.host, port=args.port) as client:
        info = client.info
        print(f"  Tuner: {info.tuner_type}, gain count: {info.gain_count}")
        _configure_receiver(client, args.fs, args.gain)
        client.set_frequency(int(center_hz))

        recv_t0 = time.time()
        print(f"\nRecording {args.duration}s @ {center_hz / 1e6:.3f} MHz wide…")
        iq_wide = client.capture_fresh(
            args.duration, args.fs, settle_s=getattr(args, "settle_seconds", 0.3)
        ).iq
        recv_dt = time.time() - recv_t0
        power = 10 * np.log10(np.mean(np.abs(iq_wide) ** 2) + 1e-12)
        print(f"  samples: {len(iq_wide):,}  power: {power:+.1f} dBFS  ({recv_dt:.1f}s)")
        _print_capture_quality(iq_wide)

    if args.save:
        dsp.write_iq_complex64(iq_wide, args.save)
        print(f"  saved wide IQ: {args.save}")

    channels = [ChannelSpec(freq_hz=f * 1e6, label=f"{f:.2f} MHz") for f in freqs_mhz]
    t0 = time.time()
    results = decode_channels(
        iq_wide=iq_wide,
        fs_wide=args.fs,
        f_center=center_hz,
        channels=channels,
        max_workers=min(len(channels), 4),
        progress=(lambda m: print(f"  ▸ {m}")) if args.verbose else None,
    )
    print(f"\nDecoding took {time.time() - t0:.1f}s. Results:\n")
    _print_results_table(results)
    return 0


def _hop_one_station(
    client: RtlTcpClient,
    freq_mhz: float,
    fs: int,
    args: argparse.Namespace,
    idx: int,
    total: int,
):
    """Tune to ``freq_mhz``, record, decode, and print one HOP-mode line.

    Returns ``(decode_result, iq_length, snapshot_timestamp_str)``.
    """
    t0 = time.time()
    client.set_frequency(int(freq_mhz * 1e6))
    capture = client.capture_fresh(args.duration, fs, settle_s=args.settle_seconds)
    iq = capture.iq
    power = 10 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-12)
    res = decode_iq(iq, fs=fs, capture_start_monotonic_ns=capture.capture_start_monotonic_ns)
    elapsed = time.time() - t0
    snapshot = datetime.now(UTC).strftime("%H:%M:%S")
    print(
        f"  [{idx}/{total}] {freq_mhz:6.2f} MHz: "
        f"power={power:+5.1f} dBFS  groups={res.n_groups:4d}  "
        f"PS='{res.info.ps_name}'  ({elapsed:.1f}s)"
    )
    return res, len(iq), snapshot


def _multi_hop(freqs_mhz: list[float], args: argparse.Namespace) -> int:
    """HOP mode: sequentially tune → record → decode each station.

    Used when the station spread exceeds the SDR's sample rate. Snapshots
    are separated in time by roughly ``duration`` seconds.
    """
    fs = 250_000  # standard single-station sample rate
    print(f"Multi-station decoder (HOP mode — stations span > {args.fs / 1e6:.2f} MS/s)")
    print(f"  Stations:       {', '.join(f'{f:.2f} MHz' for f in freqs_mhz)}")
    print(f"  Per-station fs: {fs} S/s")
    print(f"  Duration/stn:   {args.duration}s")
    print(f"  Settle discard: {args.settle_seconds}s before each capture")
    print(
        f"  Capture time:   at least {(args.duration + args.settle_seconds) * len(freqs_mhz):.0f}s"
    )
    print()

    print(f"Connecting to rtl_tcp {args.host}:{args.port}…")
    results: list[ChannelDecodeResult] = []
    snapshot_times: list[str] = []
    with RtlTcpClient(host=args.host, port=args.port) as client:
        info = client.info
        print(f"  Tuner: {info.tuner_type}, gain count: {info.gain_count}")
        _configure_receiver(client, fs, args.gain)

        for i, freq in enumerate(freqs_mhz, 1):
            res, iq_len, snapshot = _hop_one_station(client, freq, fs, args, i, len(freqs_mhz))
            spec = ChannelSpec(freq_hz=freq * 1e6, label=f"{freq:.2f} MHz")
            snapshot_times.append(snapshot)
            results.append(ChannelDecodeResult(spec=spec, iq_samples=iq_len, result=res))

    print(
        "\nSnapshots: "
        + ", ".join(f"{f:.2f} MHz @ {t}" for f, t in zip(freqs_mhz, snapshot_times, strict=False))
        + "\n"
    )
    _print_results_table(results)
    return 0


def cmd_multi(args: argparse.Namespace) -> int:
    """Multi-station: choose WIDE if all stations fit in ``fs``, otherwise HOP."""
    freqs_mhz = [float(x) for x in args.freqs.split(",")]
    freqs_hz = [f * 1e6 for f in freqs_mhz]
    if not freqs_hz:
        print("Provide at least one frequency via --freqs")
        return 2

    center_hz = args.center * 1e6 if args.center else auto_center(freqs_hz)
    fits = fits_in_window(freqs_hz, args.fs)
    mode = args.mode
    if mode == "auto":
        mode = "wide" if fits else "hop"

    if mode == "wide":
        if not fits:
            min_bw = required_bandwidth(freqs_hz)
            print(
                f"  warning: --mode wide but required BW {min_bw / 1e6:.2f} MHz "
                f"exceeds --fs {args.fs / 1e6:.2f} MHz"
            )
            print("  switch to --mode hop or increase --fs.")
            return 2
        return _multi_wide(freqs_mhz, center_hz, args)
    return _multi_hop(freqs_mhz, args)


# ---------- demo (offline multi-station showcase) ----------


_DEMO_STATION_TABLE = (
    # (PI,    PS,         freq_MHz, label,           time_offset_min)
    (0x3203, "RMF FM  ", 95.0, "RMF FM", 0),
    (0x3F44, "POLSKAR ", 96.5, "Polskie Radio", -1),
    (0x4321, "ANTYRADO", 97.5, "Antyradio", +2),
)


def _demo_resolve_now(arg_time: str | None) -> datetime:
    if arg_time:
        ts = datetime.fromisoformat(arg_time)
    else:
        ts = datetime.now(UTC).replace(second=0, microsecond=0)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def _demo_print_header(now: datetime, fs_wide: int, f_center_mhz: float, duration: float) -> None:
    print("=" * 78)
    print("  rdsclock — multi-station synthetic demonstration")
    print("=" * 78)
    print(f"  Demo time (UTC):    {now.isoformat()}")
    print("  Stations:           " + ", ".join(f"{s[3]} @{s[2]} MHz" for s in _DEMO_STATION_TABLE))
    print(f"  Simulation center:  {f_center_mhz:.2f} MHz")
    print(f"  Wide sample rate:   {fs_wide / 1e6:.2f} MS/s")
    print(f"  Duration:           {duration:.1f} s")
    print()


def _demo_synthesise_stations(now: datetime, duration: float, snr: float, seed: int) -> list:
    rng = np.random.default_rng(seed)
    station_iqs = []
    for pi, ps, freq_mhz, label, dmin in _DEMO_STATION_TABLE:
        ct_local = now + timedelta(minutes=dmin)
        groups = encode_ps_groups(pi=pi, ps_name=ps) + [
            encode_group_4a(pi=pi, clock_time_local=ct_local)
        ]
        iq = synthesize_fm_iq(groups, duration_s=duration, fs=250_000, snr_db=snr, rng=rng)
        station_iqs.append((freq_mhz, iq, ct_local, label))
        print(
            f"    ▸ {freq_mhz} MHz  PI=0x{pi:04X}  PS='{ps.strip()}'  "
            f"CT={ct_local.strftime('%H:%M')}Z"
        )
    return station_iqs


def _demo_build_wide(station_iqs: list, fs_wide: int, duration: float, f_center_hz: float):
    n_total = int(round(duration * fs_wide))
    wide = np.zeros(n_total, dtype=np.complex64)
    for freq_mhz, iq_narrow, _, _ in station_iqs:
        a, b = int(fs_wide), 250_000
        g = gcd(a, b)
        up, down = a // g, b // g
        iq_up = resample_poly(iq_narrow, up, down).astype(np.complex64)
        n_use = min(len(iq_up), n_total)
        delta = freq_mhz * 1e6 - f_center_hz
        t = np.arange(n_use) / fs_wide
        wide[:n_use] += iq_up[:n_use] * np.exp(1j * 2 * np.pi * delta * t).astype(np.complex64)
    return wide


def _demo_print_row(label: str, expected: datetime | None, decoded) -> bool:
    """Print one result row and return True when decode matches expected."""
    if decoded is not None and expected is not None:
        dt_min = (decoded.utc - expected).total_seconds() / 60.0
        ok = abs(dt_min) < 1.0
        print(
            f"  {label:<20} {expected.strftime('%H:%M Z'):<12} "
            f"{decoded.utc.strftime('%H:%M Z'):<12} {dt_min:+5.1f}m  {'OK' if ok else 'FAIL'}"
        )
        return ok
    expected_str = expected.strftime("%H:%M Z") if expected else "—"
    print(f"  {label:<20} {expected_str:<12} {'NONE':<12} {'—':<8} FAIL")
    return False


def cmd_demo(args: argparse.Namespace) -> int:
    """Self-contained demo: synthesise 3 stations with the current time,
    mix them into a wide capture, channelize, decode, and report. Ideal
    for presentations without an SDR."""
    now = _demo_resolve_now(args.time)
    f_center_mhz = sum(s[2] for s in _DEMO_STATION_TABLE) / len(_DEMO_STATION_TABLE)
    span_mhz = max(s[2] for s in _DEMO_STATION_TABLE) - min(s[2] for s in _DEMO_STATION_TABLE)
    fs_wide = max(3_500_000, int(round(span_mhz * 1.5e6)) + 1_000_000)
    duration = max(2.5, args.duration)
    f_center_hz = f_center_mhz * 1e6

    _demo_print_header(now, fs_wide, f_center_mhz, duration)

    print("  Synthesising 3 stations…")
    station_iqs = _demo_synthesise_stations(now, duration, args.snr, args.seed)

    print("  Mixing into a wide-band signal…")
    wide = _demo_build_wide(station_iqs, fs_wide, duration, f_center_hz)

    print("  Channelising and decoding each station…")
    channels = [ChannelSpec(freq_hz=s[0] * 1e6, label=f"{s[0]:.2f} MHz") for s in station_iqs]
    t0 = time.time()
    results = decode_channels(
        iq_wide=wide,
        fs_wide=fs_wide,
        f_center=f_center_hz,
        channels=channels,
        max_workers=3,
    )
    elapsed = time.time() - t0

    print()
    print("=" * 78)
    print(f"  RESULT after {elapsed:.1f}s of decoding:")
    print("=" * 78)
    print(f"  {'Station':<20} {'Expected CT':<12} {'Decoded CT':<12} {'Δt':<8} {'OK?'}")
    print(f"  {'-' * 72}")
    expected_by_freq = {s[0] * 1e6: s[2] for s in station_iqs}
    label_by_freq = {s[0] * 1e6: s[3] for s in station_iqs}
    ok_count = sum(
        _demo_print_row(
            label_by_freq.get(r.spec.freq_hz, r.spec.label),
            expected_by_freq.get(r.spec.freq_hz),
            r.result.info.latest_clock,
        )
        for r in results
    )

    print(f"  {'-' * 72}")
    print(f"  Stations with correct CT: {ok_count}/{len(results)}")
    print("=" * 78)
    return 0 if ok_count == len(results) else 1


# ---------- plot (spectrum) ----------


def cmd_plot(args: argparse.Namespace) -> int:
    """Render an annotated MPX spectrum (or IQ waterfall) of an IQ capture."""
    iq = dsp.read_iq_complex64(args.file)
    if len(iq) == 0 or np.max(np.abs(iq[:1000])) > 100:
        iq = dsp.read_iq_u8(args.file)
    title = args.title or f"{args.file}  ({args.fs / 1000:.0f} kS/s)"
    try:
        if args.kind == "waterfall":
            out = plot_iq_waterfall(iq, fs=args.fs, title=title, out_path=args.out)
        else:
            out = plot_mpx_spectrum(iq, fs=args.fs, title=title, out_path=args.out)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {out}")
    return 0


# ---------- play (FM audio) ----------


def cmd_play(args: argparse.Namespace) -> int:
    """Play mono FM audio — live from RTL-SDR or from a recorded IQ file."""
    try:
        if args.file:
            play_iq_file(args.file, fs_in=args.fs, fs_audio=args.audio_rate)
            return 0
        play_iq_live(
            freq_mhz=args.freq,
            host=args.host,
            port=args.port,
            fs_sdr=args.fs,
            fs_audio=args.audio_rate,
            gain_db=args.gain,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


# ---------- recon (continuous passive time receiver) ----------


class _EventReaderClosedError(Exception):
    """The reader of the ``--jsonl`` stream closed its end of the pipe."""


def _write_event_line(event: ReconEvent) -> None:
    line = event_to_json(event) + "\n"
    # Hold SIGINT until the whole record is out: an interrupted write would leave
    # half a line, and the recon_stopped record would be appended to it.
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except BrokenPipeError as exc:
        raise _EventReaderClosedError from exc
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _print_stderr(message: str) -> None:
    print(message, file=sys.stderr)


class _RescanRequests:
    """SIGUSR1 deliveries, consumed by the live recon loop between cycles.

    The handler only counts and the loop only records what it has seen, so a
    signal that arrives while the loop polls is never lost.
    """

    def __init__(self) -> None:
        self.received = 0
        self.seen = 0

    def handle(self, signum: int, frame: object) -> None:
        self.received += 1

    def consume(self) -> bool:
        received = self.received
        if received == self.seen:
            return False
        self.seen = received
        return True


@contextlib.contextmanager
def _rescan_on_sigusr1() -> Iterator[Callable[[], bool]]:
    """While active, SIGUSR1 asks the live loop for a new acquisition scan."""
    requests = _RescanRequests()
    previous = signal.signal(signal.SIGUSR1, requests.handle)
    try:
        yield requests.consume
    finally:
        signal.signal(signal.SIGUSR1, previous)


def _recon_jsonl(cfg: ReconConfig, args: argparse.Namespace) -> int:
    """Events go to stdout, one JSON object per line; operator text to stderr."""
    if args.from_dir:
        print("error: --jsonl applies to live reception, not --from-dir", file=sys.stderr)
        return 2
    on_progress = (lambda m: _print_stderr(f"  ▸ {m}")) if args.verbose else None
    try:
        with _rescan_on_sigusr1() as rescan_requested:
            run_recon(
                cfg,
                on_status=_print_stderr,
                on_progress=on_progress,
                on_event=_write_event_line,
                rescan_requested=rescan_requested,
            )
    except _EventReaderClosedError:
        # Stop quietly. Point stdout at /dev/null so the interpreter's final
        # flush does not report the closed pipe a second time.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
        return 1
    return 0


def _port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{value} is not a TCP port (0-65535)")
    return port


# Receiver settings that the dashboard passes on to its ``recon --jsonl`` child.
_RECON_CHILD_OPTIONS = (
    ("--start", "start"),
    ("--end", "end"),
    ("--step", "step"),
    ("--scan-dwell", "scan_dwell"),
    ("--dwell", "dwell"),
    ("--idle", "idle"),
    ("--rescan-min", "rescan_min"),
    ("--max-stations", "max_stations"),
    ("--rssi-threshold", "rssi_threshold"),
    ("--precision", "precision"),
    ("--iterations", "iterations"),
    ("--host", "host"),
    ("--port", "port"),
    ("--gain", "gain"),
    ("--settle-seconds", "settle_seconds"),
    ("--hop", "hop"),
    ("--wide", "wide"),
    ("--center", "center"),
    ("--fs", "fs"),
    ("--ct-timezone", "ct_timezone"),
)


def _recon_child_argv(args: argparse.Namespace) -> list[str]:
    """Arguments for a ``recon --jsonl`` child with the same receiver settings.

    A repeatable option is passed once per value.
    """
    argv = ["recon", "--jsonl"]
    for flag, dest in _RECON_CHILD_OPTIONS:
        value = getattr(args, dest)
        values = value if isinstance(value, list) else [value]
        argv.extend(f"{flag}={item}" for item in values if item is not None)
    return argv


def _recon_tui(args: argparse.Namespace) -> int:
    """Run the terminal dashboard, which reads a ``recon --jsonl`` child process."""
    if args.from_dir:
        print(
            translate(
                args.lang, "error: {flag} applies to live reception, not --from-dir", flag="--tui"
            ),
            file=sys.stderr,
        )
        return 2
    if importlib.util.find_spec("textual") is None:
        print(
            translate(
                args.lang,
                "error: the dashboard requires the optional 'textual' dependency; "
                "install with: pip install 'rdsclock[tui]'",
            ),
            file=sys.stderr,
        )
        return 2
    from .tui import run_dashboard

    return run_dashboard(
        [sys.executable, "-m", "rdsclock", *_recon_child_argv(args)], language=args.lang
    )


def _recon_web(args: argparse.Namespace) -> int:
    """Serve the browser dashboard, which reads a ``recon --jsonl`` child process."""
    if args.from_dir:
        print(
            translate(
                args.lang, "error: {flag} applies to live reception, not --from-dir", flag="--web"
            ),
            file=sys.stderr,
        )
        return 2
    from .web import run_web

    command = [sys.executable, "-m", "rdsclock", *_recon_child_argv(args)]
    return run_web(command, args.web_host, args.web_port, language=args.lang)


def _recon_gui(args: argparse.Namespace) -> int:
    """Open the desktop window, which reads a ``recon --jsonl`` child process."""
    if args.from_dir:
        print(
            translate(
                args.lang, "error: {flag} applies to live reception, not --from-dir", flag="--gui"
            ),
            file=sys.stderr,
        )
        return 2
    # Import rather than look the package up: tkinter can be present without
    # its native _tkinter extension or the Tcl/Tk libraries it loads.
    try:
        from .gui import run_gui
    except ImportError as exc:
        print(
            translate(
                args.lang,
                "error: the desktop window requires tkinter ({error}); install your "
                "Python's Tk support (for example: apt install python3-tk)",
                error=exc,
            ),
            file=sys.stderr,
        )
        return 2
    return run_gui([sys.executable, "-m", "rdsclock", *_recon_child_argv(args)], language=args.lang)


def _recon_config(args: argparse.Namespace) -> ReconConfig:
    return ReconConfig(
        band_start_mhz=args.start,
        band_end_mhz=args.end,
        scan_step_mhz=args.step,
        scan_dwell_s=args.scan_dwell,
        rssi_threshold_db=args.rssi_threshold,
        max_watchlist=args.max_stations,
        dwell_s=args.dwell,
        idle_s=args.idle,
        rescan_min=args.rescan_min,
        mission_precision_s=args.precision,
        gain_db=args.gain,
        settle_s=args.settle_seconds,
        host=args.host,
        port=args.port,
        iterations=args.iterations,
        # Every listed tuning; with named receivers, hop_stations replaces it.
        hop_freqs_hz=(
            ()
            if args.hop is None
            else tuple(float(item.rpartition("@")[2]) * 1e6 for item in args.hop.split(","))
        ),
        hop_stations=() if args.hop is None else _hop_stations(args.hop, args.host, args.port),
        wide_freqs_hz=() if args.wide is None else _station_freqs_hz(args.wide),
        wide_center_hz=None if args.center is None else args.center * 1e6,
        wide_sample_rate=WIDE_SAMPLE_RATE if args.fs is None else args.fs,
        ct_policy=_ct_policy(args),
    )


def _ct_policy(args: argparse.Namespace) -> CTPolicy:
    """The ``--ct-timezone`` rules; ``ValueError`` when two of them conflict."""
    return CTPolicy(tuple(args.ct_timezone or ()))


def _ct_timezone_arg(value: str) -> CTRule:
    try:
        return parse_ct_timezone(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _station_freqs_hz(text: str) -> tuple[float, ...]:
    """The stations of ``--hop`` or ``--wide`` in Hz; ``ValueError`` for an unusable list."""
    freqs_hz = tuple(float(part) * 1e6 for part in text.split(","))
    if not all(math.isfinite(freq_hz) and freq_hz > 0 for freq_hz in freqs_hz):
        raise ValueError("frequencies must be positive numbers in MHz")
    if len(set(freqs_hz)) != len(freqs_hz):
        raise ValueError("list each station once")
    return freqs_hz


def _hop_stations(
    text: str, host: str, port: int
) -> tuple[tuple[RtlTcpAddress | None, float], ...]:
    """The ``--hop`` items ``MHZ`` or ``HOST:PORT@MHZ`` with their receivers.

    Empty when no item names a receiver: every station then goes to
    ``--host``/``--port``, as before receivers could be named. ``ValueError``
    for an unusable list, including a station listed twice for one receiver.
    """
    default = RtlTcpAddress(host, port)
    stations: list[tuple[RtlTcpAddress | None, float]] = []
    seen: set[tuple[RtlTcpAddress, float]] = set()
    for item in text.split(","):
        receiver, at, mhz = item.strip().rpartition("@")
        address = RtlTcpAddress.parse(receiver) if at else None
        freq_hz = float(mhz) * 1e6
        if not (math.isfinite(freq_hz) and freq_hz > 0):
            raise ValueError("frequencies must be positive numbers in MHz")
        key = (address or default, freq_hz)
        if key in seen:
            raise ValueError(f"list each station once per receiver: {mhz} MHz on {key[0]}")
        seen.add(key)
        stations.append((address, freq_hz))
    if all(address is None for address, _ in stations):
        return ()
    return tuple(stations)


def _recon_list_error(args: argparse.Namespace) -> str | None:
    """Why the HOP or WIDE station options cannot be used, or ``None`` when they can."""
    if args.hop is not None:
        if args.wide is not None:
            return "choose either --hop or --wide"
        if args.from_dir:
            return "--hop applies to live reception, not --from-dir"
        try:
            _hop_stations(args.hop, args.host, args.port)
        except ValueError as exc:
            return f"--hop: {exc}"
    if args.wide is None:
        if args.center is not None or args.fs is not None:
            return "--center and --fs apply only to --wide"
        return None
    if args.from_dir:
        return "--wide applies to live reception, not --from-dir"
    if "@" in args.wide:
        return (
            "--wide records every station with one receiver, --host/--port; "
            "HOST:PORT@MHZ applies only to --hop"
        )
    try:
        freqs_hz = _station_freqs_hz(args.wide)
    except ValueError as exc:
        return f"--wide: {exc}"
    if args.center is not None and not (math.isfinite(args.center) and args.center > 0):
        return "--center must be a positive frequency in MHz"
    fs = WIDE_SAMPLE_RATE if args.fs is None else args.fs
    if fs < dsp.DEFAULT_INPUT_FS:
        return f"--fs must be at least {dsp.DEFAULT_INPUT_FS} S/s"
    if args.center is None:
        if not fits_in_window(freqs_hz, fs):
            return (
                f"the --wide stations need {required_bandwidth(freqs_hz) / 1e6:.2f} MS/s "
                f"with guard bands, more than --fs {fs / 1e6:.2f} MS/s"
            )
        return None
    outside = outside_capture(freqs_hz, fs, args.center * 1e6)
    if outside:
        return (
            f"{outside[0] / 1e6:.2f} MHz is too close to the edge of a {fs / 1e6:.2f} MS/s "
            f"capture centred at {args.center:.3f} MHz"
        )
    return None


def cmd_recon(args: argparse.Namespace) -> int:
    """Continuous passive time receiver: scan → hop (or one WIDE capture) → consensus."""
    error = _recon_list_error(args)
    if error is None:
        try:
            _ct_policy(args)
        except ValueError as exc:
            error = str(exc)
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.tui:
        return _recon_tui(args)
    if args.web:
        return _recon_web(args)
    if args.gui:
        return _recon_gui(args)
    cfg = _recon_config(args)
    if args.jsonl:
        return _recon_jsonl(cfg, args)
    on_progress = (lambda m: print(f"  ▸ {m}")) if args.verbose else None
    if args.from_dir:
        run_recon_offline(cfg, args.from_dir, on_progress=on_progress, limit_files=args.limit_files)
    else:
        with _rescan_on_sigusr1() as rescan_requested:
            run_recon(cfg, on_progress=on_progress, rescan_requested=rescan_requested)
    return 0


# ---------- main ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rdsclock", description="RDS Clock-Time decoder")
    p.set_defaults(verbose=False)
    sub = p.add_subparsers(dest="command", required=True)

    # generate
    pg = sub.add_parser("generate", help="Synthesise an FM IQ file with RDS Clock-Time")
    pg.add_argument("output", help="Output .iq path")
    pg.add_argument("--time", default=None, help="ISO timestamp to embed in CT (default: now)")
    pg.add_argument("--duration", type=float, default=2.0, help="Duration in seconds")
    pg.add_argument("--fs", type=int, default=DEFAULT_FS, help="IQ sample rate (Hz)")
    pg.add_argument("--snr", type=float, default=20.0, help="SNR in dB (None disables noise)")
    pg.add_argument("--no-noise", action="store_true", help="Disable noise (same as --snr None)")
    pg.add_argument("--offset", type=float, default=0.0, help="Carrier offset in Hz")
    pg.add_argument("--pi", default="0xCAFE", help="PI code (hex or decimal)")
    pg.add_argument("--ps", default="TESTSDR ", help="PS name (8 chars)")
    pg.add_argument("--pty", type=int, default=1, help="PTY value (0..31)")
    pg.add_argument("--audio-tone", type=float, default=None, help="Mono audio tone (Hz)")
    pg.add_argument("--seed", type=int, default=None, help="RNG seed for the noise generator")
    pg.add_argument(
        "--format",
        choices=["u8", "complex64"],
        default="complex64",
        help="Output format (u8 = rtl_sdr, complex64 = native)",
    )
    pg.set_defaults(func=cmd_generate)

    # decode
    pd = sub.add_parser("decode", help="Decode the clock from an IQ file")
    pd.add_argument("file", help="Path to a .iq file (uint8 or complex64)")
    pd.add_argument("--fs", type=int, default=dsp.DEFAULT_INPUT_FS, help="Sample rate")
    pd.add_argument(
        "--carrier-hz",
        type=float,
        default=None,
        dest="carrier_hz",
        help="Override the RDS subcarrier frequency",
    )
    pd.add_argument("-v", "--verbose", action="store_true", help="Print pipeline steps")
    pd.add_argument(
        "--capture-start-iso",
        default=None,
        dest="capture_start_iso",
        help="Optional ISO timestamp for the start of an offline capture",
    )
    pd.set_defaults(func=cmd_decode)

    # live
    pl = sub.add_parser("live", help="Record from RTL-SDR and decode")
    pl.add_argument("--freq", type=float, default=95.5, help="Frequency (MHz)")
    pl.add_argument("--duration", type=float, default=10.0, help="Recording duration (s)")
    pl.add_argument("--fs", type=int, default=dsp.DEFAULT_INPUT_FS, help="Sample rate")
    pl.add_argument("--host", default="localhost")
    pl.add_argument("--port", type=int, default=1234)
    pl.add_argument("--gain", type=float, default=None, help="Manual gain (dB), default AGC")
    pl.add_argument("--ppm", type=int, default=0, help="Tuner PPM correction")
    pl.add_argument(
        "--settle-seconds",
        type=float,
        default=3.0,
        dest="settle_seconds",
        help="Receive and discard IQ for this long after retuning, before the full recording duration",
    )
    pl.add_argument(
        "--carrier-hz",
        type=float,
        default=None,
        dest="carrier_hz",
        help="Fixed subcarrier frequency (instead of pilot-based auto-detect)",
    )
    pl.add_argument("--save", default=None, help="Save IQ to this file")
    pl.add_argument("-v", "--verbose", action="store_true")
    pl.set_defaults(func=cmd_live)

    # multi
    pm = sub.add_parser(
        "multi",
        help="Multi-station: auto WIDE if stations fit in fs, otherwise HOP",
    )
    pm.add_argument(
        "--freqs",
        required=True,
        help="Comma-separated frequencies in MHz, e.g. 92.0,98.3,106.8",
    )
    pm.add_argument(
        "--mode",
        choices=["auto", "wide", "hop"],
        default="auto",
        help="auto = WIDE when stations fit in fs, otherwise HOP",
    )
    pm.add_argument(
        "--duration",
        type=float,
        default=8.0,
        help="Recording duration (wide: one capture; hop: per station)",
    )
    pm.add_argument(
        "--fs", type=int, default=2_400_000, help="WIDE-mode sample rate (e.g. 2.4 MS/s)"
    )
    pm.add_argument(
        "--center",
        type=float,
        default=None,
        help="WIDE-mode centre frequency (default: midpoint of stations)",
    )
    pm.add_argument("--host", default="localhost")
    pm.add_argument("--port", type=int, default=1234)
    pm.add_argument("--gain", type=float, default=None)
    pm.add_argument(
        "--settle-seconds",
        type=float,
        default=3.0,
        dest="settle_seconds",
        help="Receive and discard IQ after tuning, before each full recording duration",
    )
    pm.add_argument("--save", default=None, help="Save wide IQ to file (WIDE only)")
    pm.add_argument("-v", "--verbose", action="store_true")
    pm.set_defaults(func=cmd_multi)

    # demo
    pdm = sub.add_parser("demo", help="Self-contained 3-station multi-channel showcase (no SDR)")
    pdm.add_argument("--time", default=None, help="ISO timestamp (default: now)")
    pdm.add_argument("--duration", type=float, default=3.0, help="Synthetic duration (s)")
    pdm.add_argument("--snr", type=float, default=25.0, help="Synthetic SNR (dB)")
    pdm.add_argument("--seed", type=int, default=42, help="RNG seed")
    pdm.set_defaults(func=cmd_demo)

    # recon (continuous passive time receiver)
    pr = sub.add_parser(
        "recon",
        help="Passive time receiver — multi-source consensus + hop scan or WIDE capture",
    )
    pr.add_argument("--start", type=float, default=87.5, help="Band start (MHz)")
    pr.add_argument("--end", type=float, default=108.0, help="Band end (MHz)")
    pr.add_argument("--step", type=float, default=0.2, help="Scan step (MHz)")
    pr.add_argument(
        "--scan-dwell",
        type=float,
        default=1.5,
        dest="scan_dwell",
        help="Per-sample dwell during scan (s)",
    )
    pr.add_argument(
        "--dwell", type=float, default=8.0, help="Per-station dwell during hop loop (s)"
    )
    pr.add_argument("--idle", type=float, default=1.0, help="Sleep between iterations (s)")
    pr.add_argument(
        "--rescan-min",
        type=float,
        default=10.0,
        dest="rescan_min",
        help="Full rescan period (minutes)",
    )
    pr.add_argument(
        "--max-stations",
        type=int,
        default=5,
        dest="max_stations",
        help="Maximum watchlist size after a band scan (--hop and --wide keep every station)",
    )
    pr.add_argument(
        "--rssi-threshold",
        type=float,
        default=-20.0,
        dest="rssi_threshold",
        help="Minimum RSSI (dBFS) to consider a station",
    )
    pr.add_argument("--precision", type=float, default=60.0, help="Target precision in seconds")
    pr.add_argument("--iterations", type=int, default=None, help="Iteration limit (for tests)")
    pr.add_argument("--host", default="localhost")
    pr.add_argument("--port", type=int, default=1234)
    pr.add_argument("--gain", type=float, default=None)
    pr.add_argument(
        "--settle-seconds", type=float, default=0.3, help="Discard incoming IQ after retuning (s)"
    )
    pr.add_argument(
        "--hop",
        default=None,
        metavar="STATIONS",
        help="Hop between these stations instead of scanning the band: comma-separated "
        "MHZ, or HOST:PORT@MHZ to record that station with another rtl_tcp receiver "
        "(IPv6 as [::1]:1234@98.8); other stations use --host/--port. Each receiver hops "
        "through its own stations, all receivers at the same time",
    )
    pr.add_argument(
        "--wide",
        default=None,
        metavar="FREQS",
        help="Record these stations (MHz, comma-separated) together from one wideband "
        "capture per cycle, instead of scanning the band and hopping",
    )
    pr.add_argument(
        "--center",
        type=float,
        default=None,
        help="WIDE capture centre in MHz (default: near the middle, with every station "
        "clear of the DC offset and of the other stations' mirror images)",
    )
    pr.add_argument(
        "--fs",
        type=int,
        default=None,
        help=f"WIDE capture sample rate in S/s (default {WIDE_SAMPLE_RATE})",
    )
    pr.add_argument(
        "--ct-timezone",
        type=_ct_timezone_arg,
        action="append",
        default=None,
        dest="ct_timezone",
        metavar="SPEC",
        help="For stations that violate the RDS standard by sending local time in the "
        "Clock-Time UTC field: read it as wall-clock time of an IANA zone and convert it "
        "to UTC before the consensus. SPEC is ZONE (every station), pi:0x3211=ZONE or "
        "freq:98.8=ZONE (MHz); a PI rule wins over a frequency rule, which wins over ZONE. "
        "ZONE is UTC (standard) or an IANA name such as Europe/Warsaw. Repeatable",
    )
    pr.add_argument(
        "--from-dir",
        default=None,
        dest="from_dir",
        help="OFFLINE: replay recon over IQ files in this directory (no SDR)",
    )
    pr.add_argument(
        "--limit-files",
        type=int,
        default=None,
        dest="limit_files",
        help="OFFLINE: maximum number of files to process",
    )
    output = pr.add_mutually_exclusive_group()
    output.add_argument(
        "--jsonl",
        action="store_true",
        help="Write events to stdout as JSON Lines; operator text goes to stderr",
    )
    output.add_argument(
        "--tui",
        action="store_true",
        help="Show a terminal dashboard (optional 'tui' extra) fed by a --jsonl child",
    )
    output.add_argument(
        "--web",
        action="store_true",
        help="Serve a browser dashboard fed by a --jsonl child (no authentication)",
    )
    output.add_argument(
        "--gui",
        action="store_true",
        help="Open a desktop window (tkinter) fed by a --jsonl child",
    )
    pr.add_argument(
        "--lang",
        choices=SUPPORTED_LANGUAGES,
        default="en",
        help="Language of the terminal, browser or desktop dashboard (default: en)",
    )
    pr.add_argument(
        "--web-host",
        default="127.0.0.1",
        dest="web_host",
        help="Address for --web (default 127.0.0.1; use an SSH tunnel for remote access)",
    )
    pr.add_argument("--web-port", type=_port, default=8080, dest="web_port", help="Port for --web")
    pr.add_argument("-v", "--verbose", action="store_true")
    pr.set_defaults(func=cmd_recon)

    # plot (spectrum / waterfall)
    pplot = sub.add_parser(
        "plot",
        help="Render an annotated MPX spectrum or IQ waterfall (PNG)",
    )
    pplot.add_argument("file", help="IQ file to analyse (uint8 or complex64)")
    pplot.add_argument(
        "--kind",
        choices=["mpx", "waterfall"],
        default="mpx",
        help="mpx = FM baseband FFT with annotated bands; waterfall = IQ spectrogram",
    )
    pplot.add_argument("--fs", type=int, default=dsp.DEFAULT_INPUT_FS, help="Capture sample rate")
    pplot.add_argument("--out", default=None, help="Output PNG path")
    pplot.add_argument("--title", default=None, help="Figure title override")
    pplot.set_defaults(func=cmd_plot)

    # play (live FM audio or file playback)
    pp = sub.add_parser(
        "play",
        help="Play mono FM audio — live from RTL-SDR or from a recorded IQ file",
    )
    pp.add_argument(
        "--freq", type=float, default=93.3, help="Live FM frequency (MHz); ignored with --file"
    )
    pp.add_argument("--file", default=None, help="Play back a recorded IQ file instead of live")
    pp.add_argument(
        "--fs",
        type=int,
        default=DEFAULT_LIVE_FS,
        help="SDR sample rate; for --file use the file's capture rate (e.g. 250000)",
    )
    pp.add_argument(
        "--audio-rate",
        type=int,
        default=DEFAULT_AUDIO_RATE,
        dest="audio_rate",
        help="Output audio sample rate (Hz)",
    )
    pp.add_argument("--host", default="localhost")
    pp.add_argument("--port", type=int, default=1234)
    pp.add_argument("--gain", type=float, default=None, help="Manual gain dB (default AGC)")
    pp.set_defaults(func=cmd_play)

    # scan
    ps = sub.add_parser("scan", help="Sweep the FM band and find stations with CT")
    ps.add_argument("--start", type=float, default=87.5)
    ps.add_argument("--end", type=float, default=108.0)
    ps.add_argument("--step", type=float, default=0.1)
    ps.add_argument("--duration", type=float, default=5.0, help="Per-station dwell (s)")
    ps.add_argument("--fs", type=int, default=dsp.DEFAULT_INPUT_FS)
    ps.add_argument("--host", default="localhost")
    ps.add_argument("--port", type=int, default=1234)
    ps.add_argument("--gain", type=float, default=None)
    ps.add_argument(
        "--settle-seconds", type=float, default=0.3, help="Discard incoming IQ after retuning (s)"
    )
    ps.set_defaults(func=cmd_scan)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "no_noise", False):
        args.snr = None
    try:
        return args.func(args)
    except OSError as exc:
        print(f"rdsclock: {exc}", file=sys.stderr)
        if args.command in {"live", "scan", "multi", "recon", "play"}:
            print(
                "Check rtl_tcp availability and whether another client is using the receiver.",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
