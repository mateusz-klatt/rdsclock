"""Command-line interface: ``python -m rdsclock <command> ...``.

Sub-commands:
    generate   — synthesise an IQ file with an embedded clock (for tests/demo)
    decode     — decode the clock from an IQ file
    live       — connect to rtl_tcp, record N seconds, decode
    scan       — sweep the FM band, attempt to decode each station
    multi      — multi-station decode (auto WIDE if in range, otherwise HOP)
    demo       — self-contained 3-station multi-channel demo (no SDR needed)
    recon      — continuous passive time receiver (live or offline mode)
"""

import argparse
import os
import sys
import time
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
    required_bandwidth,
)
from .decoder import decode_file, decode_iq
from .plot import plot_iq_waterfall, plot_mpx_spectrum
from .rds_groups import StationInfo, encode_group_4a, encode_ps_groups
from .recon import ReconConfig, run_recon, run_recon_offline
from .rtl_tcp import RtlTcpClient
from .synth import DEFAULT_FS, synthesize_fm_iq

# ---------- helpers ----------

_GAIN_AGC_LINE = "  Gain: AGC"


def _configure_gain(client: RtlTcpClient, gain_db: float | None) -> None:
    """Apply manual gain in dB or fall back to AGC and print one banner line."""
    if gain_db is None:
        client.set_gain_mode_auto()
        print(_GAIN_AGC_LINE)
    else:
        client.set_gain_mode_manual(int(gain_db * 10))
        print(f"  Gain: manual {gain_db} dB")


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
        print("  ClockTime: NONE (station not transmitting Group 4A or sending dummy data)")
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
    print(f"  Δf offset:  {result.freq_offset_hz:+.1f} Hz")
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
        client.set_sample_rate(args.fs)
        if args.ppm:
            client.set_ppm(args.ppm)
            print(f"  PPM correction: {args.ppm}")
        client.set_frequency(int(args.freq * 1e6))
        _configure_gain(client, args.gain)

        print(f"Recording {args.duration}s @ {args.freq} MHz …")
        capture_start_ns = time.monotonic_ns()
        iq = client.record(args.duration, args.fs)
        print(
            f"  samples: {len(iq)}  power: "
            f"{10 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-12):.1f} dBFS"
        )

    if args.save:
        dsp.write_iq_complex64(iq, args.save)
        print(f"  saved: {args.save}")

    iq_decode, n_trimmed = _trim_settle_iq(iq, args.fs, args.settle_seconds)
    capture_decode_start_ns = capture_start_ns + int(round(n_trimmed * 1_000_000_000 / args.fs))

    result = decode_iq(
        iq_decode,
        fs=args.fs,
        carrier_hz=args.carrier_hz,
        auto_carrier=args.carrier_hz is None,
        progress=(lambda m: print(f"  ▸ {m}")) if args.verbose else None,
        capture_start_monotonic_ns=capture_decode_start_ns,
    )
    print(
        f"\n  Groups: {result.n_groups}  Bits: {result.n_bits}  Δf={result.freq_offset_hz:+.1f} Hz"
    )
    _print_station(result.info)
    return 0


# ---------- scan ----------


def _scan_one_frequency(client: RtlTcpClient, freq_mhz: float, args: argparse.Namespace) -> str:
    """Capture, decode and pretty-print a single scan line. Returns the line."""
    client.set_frequency(int(freq_mhz * 1e6))
    iq = client.record(args.duration, args.fs)
    power_db = 10 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-12)
    decoded = decode_iq(iq, fs=args.fs)
    mark = _scan_mark(decoded)
    has_ct = "YES" if decoded.info.latest_clock is not None else "—"
    line = (
        f"{mark} {freq_mhz:6.2f} MHz  {power_db:+6.1f} dBFS  "
        f"groups={decoded.n_groups:4d}  PS='{decoded.info.ps_name:<8}'  "
        f"CT={has_ct}"
    )
    print(line)
    return line


def cmd_scan(args: argparse.Namespace) -> int:
    print(f"Scanning {args.start}-{args.end} MHz, step {args.step} MHz @ {args.fs} S/s…")
    with RtlTcpClient(host=args.host, port=args.port) as client:
        client.set_sample_rate(args.fs)
        if args.gain is None:
            client.set_gain_mode_auto()
        else:
            client.set_gain_mode_manual(int(args.gain * 10))

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
        print("\nNo station in the scanned band transmitted a valid Clock-Time.")
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
        client.set_sample_rate(args.fs)
        client.set_frequency(int(center_hz))
        _configure_gain(client, args.gain)

        recv_t0 = time.time()
        print(f"\nRecording {args.duration}s @ {center_hz / 1e6:.3f} MHz wide…")
        iq_wide = client.record(args.duration, args.fs)
        recv_dt = time.time() - recv_t0
        power = 10 * np.log10(np.mean(np.abs(iq_wide) ** 2) + 1e-12)
        print(f"  samples: {len(iq_wide):,}  power: {power:+.1f} dBFS  ({recv_dt:.1f}s)")

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
    iq = client.record(args.duration, fs)
    power = 10 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-12)
    iq_decode, _ = _trim_settle_iq(iq, fs, args.settle_seconds)
    res = decode_iq(iq_decode, fs=fs)
    elapsed = time.time() - t0
    snapshot = datetime.now(UTC).strftime("%H:%M:%S")
    print(
        f"  [{idx}/{total}] {freq_mhz:6.2f} MHz: "
        f"power={power:+5.1f} dBFS  groups={res.n_groups:4d}  "
        f"PS='{res.info.ps_name}'  ({elapsed:.1f}s)"
    )
    return res, len(iq_decode), snapshot


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
    print(f"  Settle trim:    {args.settle_seconds}s")
    print(f"  Total time:     ~{args.duration * len(freqs_mhz):.0f}s")
    print()

    print(f"Connecting to rtl_tcp {args.host}:{args.port}…")
    results: list[ChannelDecodeResult] = []
    snapshot_times: list[str] = []
    with RtlTcpClient(host=args.host, port=args.port) as client:
        info = client.info
        print(f"  Tuner: {info.tuner_type}, gain count: {info.gain_count}")
        client.set_sample_rate(fs)
        _configure_gain(client, args.gain)

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


def cmd_recon(args: argparse.Namespace) -> int:
    """Continuous passive time receiver: scan → hop → consensus → display."""
    cfg = ReconConfig(
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
        host=args.host,
        port=args.port,
        iterations=args.iterations,
    )
    on_progress = (lambda m: print(f"  ▸ {m}")) if args.verbose else None
    if args.from_dir:
        run_recon_offline(cfg, args.from_dir, on_progress=on_progress, limit_files=args.limit_files)
    else:
        run_recon(cfg, on_progress=on_progress)
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
        help="Trim this many seconds from the start of every retune to skip RTL-SDR PLL/AGC/Costas settling",
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
        help="HOP mode: trim this many seconds from each retune to skip RTL-SDR PLL/AGC/Costas settling",
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
        help="Passive time receiver — multi-source consensus + hop scan",
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
        help="Maximum watchlist size",
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
    ps.set_defaults(func=cmd_scan)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "no_noise", False):
        args.snr = None
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
