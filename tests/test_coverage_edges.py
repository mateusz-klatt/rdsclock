"""Focused edge-coverage tests for control-flow and guard branches."""

from __future__ import annotations

import builtins
import contextlib
import runpy
import socket
import struct
import sys
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

import rdsclock.channelizer as channelizer
import rdsclock.cli as cli
import rdsclock.decoder as decoder
import rdsclock.plot as plot
import rdsclock.recon as recon
from rdsclock import dsp
from rdsclock.audio import play_iq_file, play_iq_live
from rdsclock.decoder import DecodeResult
from rdsclock.rds_blocks import (
    BLOCK_BITS,
    OFFSET_A,
    OFFSET_C,
    OFFSET_C_PRIME,
    block_valid,
    blocks_to_bits,
    count_valid_blocks,
    differential_decode,
    encode_block,
    encode_group,
    find_groups_in_bitstream,
    group_bytes_to_words,
    group_words_to_bytes,
)
from rdsclock.rds_clock import ClockTime, decode_clock_time, encode_clock_time
from rdsclock.rds_groups import StationInfo, parse_group
from rdsclock.rtl_tcp import RtlTcpClient
from rdsclock.synth import (
    DEFAULT_INTERMEDIATE_FS,
    add_awgn,
    fm_modulate,
    make_mpx,
    rds_baseband,
    rds_groups_to_bits,
    resample_to,
    synthesize_fm_iq,
)


def _clock(minutes: int = 0) -> ClockTime:
    return ClockTime(
        utc=datetime(2026, 5, 17, 12, minutes, tzinfo=UTC),
        local_offset_minutes=0,
    )


def _decode_result(
    *,
    clock: ClockTime | None = None,
    groups: int = 1,
    pi: int | None = 0xCAFE,
    ps_name: str = "TEST",
    freq_offset_hz: float = 0.0,
) -> DecodeResult:
    info = StationInfo(pi=pi)
    info.ps_chars[: len(ps_name[:8])] = list(ps_name[:8])
    if clock is not None:
        info.clock_times.append(clock)
    return DecodeResult(
        info=info,
        n_groups=groups,
        n_bits=104 * groups,
        freq_offset_hz=freq_offset_hz,
        symbol_offset=0,
    )


class _FakeSoundDevice:
    def __init__(self, *, play_raises: bool = False):
        self.default = SimpleNamespace(samplerate=None, channels=None)
        self.play_raises = play_raises
        self.play_calls: list[tuple[np.ndarray, int, bool]] = []
        self.stopped = False
        self.stream_writes: list[np.ndarray] = []

    def play(self, audio, samplerate, blocking):
        if self.play_raises:
            raise KeyboardInterrupt
        self.play_calls.append((np.asarray(audio), samplerate, blocking))

    def stop(self):
        self.stopped = True

    def OutputStream(self, dtype):  # noqa: N802 - matches sounddevice API
        fake_sd = self

        class _Stream:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def write(self, audio):
                fake_sd.stream_writes.append(np.asarray(audio))

        assert dtype == "float32"
        return _Stream()


class _FakeAudioClient:
    def __init__(self, *_, **__):
        self.info = SimpleNamespace(tuner_type=5, gain_count=29)
        self.calls: list[tuple[str, int | None]] = []
        self.read_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_sample_rate(self, value):
        self.calls.append(("sample_rate", value))

    def set_gain_mode_auto(self):
        self.calls.append(("gain_auto", None))

    def set_gain_mode_manual(self, value):
        self.calls.append(("gain_manual", value))

    def set_frequency(self, value):
        self.calls.append(("frequency", value))

    def read_iq(self, n_samples, settle_s=0.0):
        self.read_count += 1
        if self.read_count >= 3:
            raise KeyboardInterrupt
        n = np.arange(n_samples)
        return np.exp(1j * 2 * np.pi * n / max(n_samples, 1)).astype(np.complex64)


def test_audio_play_file_complex64_and_u8_paths(tmp_path, monkeypatch):
    sd = _FakeSoundDevice()
    monkeypatch.setitem(sys.modules, "sounddevice", sd)

    n = np.arange(512)
    iq = np.exp(1j * 2 * np.pi * n / 64).astype(np.complex64)
    complex_path = tmp_path / "complex.iq"
    iq.tofile(complex_path)
    play_iq_file(str(complex_path), fs_in=48_000, fs_audio=48_000)
    assert sd.play_calls[-1][1:] == (48_000, True)

    u8_path = tmp_path / "u8-ish.iq"
    np.full(512, 200 + 0j, dtype=np.complex64).tofile(u8_path)
    play_iq_file(str(u8_path), fs_in=50_000, fs_audio=48_000)
    assert sd.play_calls[-1][1] == 48_000


def test_audio_play_file_keyboard_interrupt_stops(tmp_path, monkeypatch, capsys):
    sd = _FakeSoundDevice(play_raises=True)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    path = tmp_path / "complex.iq"
    np.ones(256, dtype=np.complex64).tofile(path)

    play_iq_file(str(path), fs_in=48_000, fs_audio=48_000)

    assert sd.stopped
    assert "Interrupted" in capsys.readouterr().out


@pytest.mark.parametrize(
    "gain_db, expected_call", [(None, ("gain_auto", None)), (12.5, ("gain_manual", 125))]
)
def test_audio_play_live_streams_until_keyboard_interrupt(
    gain_db, expected_call, monkeypatch, capsys
):
    sd = _FakeSoundDevice()
    clients: list[_FakeAudioClient] = []

    class RecordingClient(_FakeAudioClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    monkeypatch.setattr("rdsclock.audio.RtlTcpClient", RecordingClient)

    play_iq_live(
        95.5,
        host="127.0.0.1",
        port=1234,
        fs_sdr=48_000,
        fs_audio=48_000,
        chunk_samples=256,
        gain_db=gain_db,
    )

    assert expected_call in clients[0].calls
    assert sd.stream_writes
    assert "Interrupted" in capsys.readouterr().out


def test_audio_play_live_supports_rational_audio_rate(monkeypatch, capsys):
    sd = _FakeSoundDevice()
    clients: list[_FakeAudioClient] = []

    class RecordingClient(_FakeAudioClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    monkeypatch.setattr("rdsclock.audio.RtlTcpClient", RecordingClient)

    play_iq_live(
        95.5,
        fs_sdr=50_000,
        fs_audio=48_000,
        chunk_samples=256,
    )

    assert ("sample_rate", 50_000) in clients[0].calls
    assert sd.stream_writes
    assert sd.stream_writes[0].dtype == np.float32
    assert "Interrupted" in capsys.readouterr().out


@pytest.mark.parametrize("func,args", [(play_iq_file, ("x.iq",)), (play_iq_live, (95.5,))])
def test_audio_play_requires_sounddevice(func, args, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *import_args, **import_kwargs):
        if name == "sounddevice":
            raise ImportError("missing sounddevice")
        return real_import(name, *import_args, **import_kwargs)

    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(RuntimeError, match="sounddevice"):
        func(*args)


def test_channelizer_edge_helpers_and_progress(monkeypatch):
    with pytest.raises(ValueError, match="fs_out"):
        channelizer.extract_channel(np.ones(16, dtype=np.complex64), 1000, 0, 0, fs_out=2000)
    with pytest.raises(ValueError, match="non-empty"):
        channelizer.auto_center([])
    with pytest.raises(ValueError, match="non-empty"):
        channelizer.required_bandwidth([])

    monkeypatch.setattr(channelizer, "decode_iq", lambda iq, fs: _decode_result(groups=0))
    messages: list[str] = []
    results = channelizer.decode_channels(
        iq_wide=np.ones(512, dtype=np.complex64),
        fs_wide=400_000,
        f_center=100e6,
        channels=[channelizer.ChannelSpec(freq_hz=100e6, label="center")],
        fs_out=100_000,
        max_workers=1,
        progress=messages.append,
    )
    assert len(results) == 1
    assert any("extract" in message for message in messages)
    assert any("decode" in message for message in messages)


def test_cli_small_branches(tmp_path, monkeypatch, capsys):
    assert (
        cli._scan_mark(SimpleNamespace(info=SimpleNamespace(latest_clock=_clock()), n_groups=0))
        == "[CT]"
    )
    assert (
        cli._scan_mark(SimpleNamespace(info=SimpleNamespace(latest_clock=None), n_groups=2))
        == "[FM]"
    )

    monkeypatch.setattr(
        cli.dsp, "read_iq_complex64", lambda path: (_ for _ in ()).throw(ValueError)
    )
    monkeypatch.setattr(cli.dsp, "read_iq_u8", lambda path: np.ones(8, dtype=np.complex64))
    monkeypatch.setattr(cli, "decode_iq", lambda *args, **kwargs: _decode_result())
    assert cli.main(["decode", str(tmp_path / "missing.iq"), "--carrier-hz", "57000"]) == 0

    class EmptyFreqs:
        def split(self, sep):
            return []

    args = SimpleNamespace(freqs=EmptyFreqs(), center=None, fs=2_400_000, mode="auto")
    assert cli.cmd_multi(args) == 2

    monkeypatch.setattr(cli, "_multi_wide", lambda freqs, center, args: 0)
    args = SimpleNamespace(freqs="95.5,96.0", center=None, fs=2_400_000, mode="auto")
    assert cli.cmd_multi(args) == 0

    now = cli._demo_resolve_now(None)
    assert now.tzinfo is UTC
    assert cli._demo_print_row("station", datetime(2026, 5, 17, 12, 0, tzinfo=UTC), None) is False
    assert "FAIL" in capsys.readouterr().out


def test_cli_scan_prints_clock_time_summary(monkeypatch, capsys):
    class FakeClient:
        def __init__(self, *_, **__):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def set_sample_rate(self, value):
            self.sample_rate = value

        def set_gain_mode_auto(self):
            self.gain = "auto"

    monkeypatch.setattr(cli, "RtlTcpClient", FakeClient)

    def fake_scan_one(client, freq_mhz, args):
        line = f"[CT] {freq_mhz:6.2f} MHz"
        print(line)
        return line

    monkeypatch.setattr(cli, "_scan_one_frequency", fake_scan_one)
    args = SimpleNamespace(
        start=95.5,
        end=95.5,
        step=0.1,
        fs=250_000,
        host="127.0.0.1",
        port=1234,
        gain=None,
    )

    assert cli.cmd_scan(args) == 0
    assert "Stations with Clock-Time" in capsys.readouterr().out


def test_cli_multi_wide_save_and_plot_play_paths(tmp_path, monkeypatch, capsys):
    class FakeClient:
        info = SimpleNamespace(tuner_type=5, gain_count=29)

        def __init__(self, *_, **__):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def set_sample_rate(self, value):
            self.sample_rate = value

        def set_frequency(self, value):
            self.frequency = value

        def set_gain_mode_auto(self):
            self.gain = "auto"

        def record(self, duration, fs):
            return np.ones(128, dtype=np.complex64)

    monkeypatch.setattr(cli, "RtlTcpClient", FakeClient)
    result = channelizer.ChannelDecodeResult(
        spec=channelizer.ChannelSpec(freq_hz=95.5e6, label="95.50 MHz"),
        iq_samples=128,
        result=_decode_result(clock=_clock()),
    )
    monkeypatch.setattr(cli, "decode_channels", lambda **kwargs: [result])
    save_path = tmp_path / "wide.iq"
    args = SimpleNamespace(
        fs=2_400_000,
        duration=0.01,
        host="127.0.0.1",
        port=1234,
        gain=None,
        save=str(save_path),
        verbose=False,
    )
    assert cli._multi_wide([95.5], 95.5e6, args) == 0
    assert save_path.exists()

    monkeypatch.setattr(
        cli.dsp, "read_iq_complex64", lambda path: np.array([200 + 0j], dtype=np.complex64)
    )
    monkeypatch.setattr(cli.dsp, "read_iq_u8", lambda path: np.ones(8, dtype=np.complex64))
    monkeypatch.setattr(cli, "plot_mpx_spectrum", lambda *args, **kwargs: "plot.png")
    plot_args = SimpleNamespace(file="x.iq", fs=250_000, title=None, kind="mpx", out=None)
    assert cli.cmd_plot(plot_args) == 0

    monkeypatch.setattr(
        cli,
        "plot_mpx_spectrum",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no plot")),
    )
    assert cli.cmd_plot(plot_args) == 2

    file_args = SimpleNamespace(file="x.iq", fs=250_000, audio_rate=48_000)
    monkeypatch.setattr(cli, "play_iq_file", lambda *args, **kwargs: None)
    assert cli.cmd_play(file_args) == 0

    live_args = SimpleNamespace(
        file=None,
        freq=95.5,
        host="127.0.0.1",
        port=1234,
        fs=48_000,
        audio_rate=48_000,
        gain=None,
    )
    monkeypatch.setattr(cli, "play_iq_live", lambda *args, **kwargs: None)
    assert cli.cmd_play(live_args) == 0
    monkeypatch.setattr(
        cli, "play_iq_live", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no audio"))
    )
    assert cli.cmd_play(live_args) == 2
    assert "error: no audio" in capsys.readouterr().err


def test_decoder_decode_file_formats_and_errors(tmp_path, monkeypatch):
    seen: list[tuple[int, float]] = []

    def fake_decode_iq(iq, fs, progress=None):
        seen.append((len(iq), fs))
        return _decode_result()

    monkeypatch.setattr(decoder, "decode_iq", fake_decode_iq)
    assert _decode_result(clock=_clock()).clock_times[0].utc.year == 2026

    complex_path = tmp_path / "complex.iq"
    np.ones(128, dtype=np.complex64).tofile(complex_path)
    assert decoder.decode_file(str(complex_path), fs=123_000).n_groups == 1
    assert seen[-1] == (128, 123_000)

    u8_path = tmp_path / "u8.iq"
    u8_path.write_bytes(bytes([0, 255, 128]))
    assert decoder.decode_file(str(u8_path), fmt="u8").n_groups == 1
    assert decoder.decode_file(str(u8_path)).n_groups == 1

    zero_path = tmp_path / "zeros.iq"
    zero_path.write_bytes(bytes(512))
    assert decoder.decode_file(str(zero_path)).n_groups == 1

    with pytest.raises(ValueError, match="unknown IQ format"):
        decoder.decode_file(str(complex_path), fmt="bad")
    empty_path = tmp_path / "empty.iq"
    empty_path.touch()
    with pytest.raises(ValueError, match="empty IQ file"):
        decoder.decode_file(str(empty_path), fmt="complex64")


def test_dsp_short_inputs_and_fallbacks(tmp_path):
    odd_path = tmp_path / "odd.u8"
    odd_path.write_bytes(bytes([0, 255, 128]))
    assert len(dsp.read_iq_u8(str(odd_path))) == 1

    assert dsp.estimate_pilot_19khz(np.ones(8, dtype=np.float32), fs=250_000) == 19_000.0
    assert dsp.estimate_pilot_19khz(np.ones(2048, dtype=np.float32), fs=1500) == 19_000.0
    assert (
        dsp.estimate_rds_carrier(np.ones(8, dtype=np.float32), fs=250_000, use_pilot=False)
        == 57_000.0
    )
    assert (
        dsp.estimate_rds_carrier(np.ones(2048, dtype=np.float32), fs=1500, use_pilot=False)
        == 57_000.0
    )

    fs = 250_000
    n = np.arange(4096)
    tone = np.cos(2 * np.pi * 58_000 * n / fs).astype(np.float32)
    estimate = dsp.estimate_rds_carrier(tone, fs=fs, around=57_000, use_pilot=False)
    assert abs(estimate - 58_000) < 100

    corrected, offset = dsp.coarse_freq_correction(np.ones(1, dtype=np.complex64), fs=19_000)
    np.testing.assert_array_equal(corrected, np.ones(1, dtype=np.complex64))
    assert offset == 0.0

    assert len(dsp.symbol_lpf(np.ones(100, dtype=np.complex64), fs=19_000)) == 100
    assert len(dsp.clock_recovery_mm(np.ones(10, dtype=np.complex64), sps=16)) == 0
    assert len(dsp.bits_from_symbols_diff(np.ones(1, dtype=np.complex64))) == 0
    assert len(dsp.decimate_to_rds_rate(np.ones(20, dtype=np.complex64), input_fs=1000)) > 20


def test_dsp_clock_recovery_stops_when_interpolator_is_exhausted(monkeypatch):
    monkeypatch.setattr(
        dsp,
        "resample_poly",
        lambda samples, up, down: np.zeros(1, dtype=np.complex64),
    )

    recovered = dsp.clock_recovery_mm(np.ones(40, dtype=np.complex64), sps=1.0)

    assert len(recovered) == 1


def test_main_module_entrypoint(monkeypatch):
    monkeypatch.setattr(cli, "main", lambda: 7)
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("rdsclock.__main__", run_name="__main__")
    assert exc.value.code == 7


def test_cli_module_entrypoint_help(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["rdsclock", "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("rdsclock.cli", run_name="__main__")
    assert exc.value.code == 0


def test_plot_import_errors_show_and_pilot_exception(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *import_args, **import_kwargs):
        if name == "matplotlib":
            raise ImportError("missing matplotlib")
        return real_import(name, *import_args, **import_kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="plotting requires"):
        plot.plot_mpx_spectrum(np.ones(2048, dtype=np.complex64), fs=250_000)
    with pytest.raises(RuntimeError, match="plotting requires"):
        plot.plot_iq_waterfall(np.ones(2048, dtype=np.complex64), fs=250_000)
    monkeypatch.setattr(builtins, "__import__", real_import)

    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    show_calls: list[bool] = []
    monkeypatch.setattr(plt, "show", lambda: show_calls.append(True))
    monkeypatch.setattr(
        plot.dsp, "estimate_pilot_19khz", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError)
    )
    iq = np.exp(1j * 2 * np.pi * 1000 * np.arange(4096) / 250_000).astype(np.complex64)
    out = tmp_path / "mpx.png"
    assert plot.plot_mpx_spectrum(iq, fs=250_000, out_path=str(out), show=True) == str(out)
    assert show_calls == [True]

    out_wf = tmp_path / "wf.png"
    assert plot.plot_iq_waterfall(
        iq, fs=250_000, out_path=str(out_wf), nperseg=512, show=True
    ) == str(out_wf)
    assert show_calls == [True, True]


def test_rds_blocks_guards_and_diagnostics():
    with pytest.raises(ValueError, match="dataword out of range"):
        encode_block(1 << 16, OFFSET_A)
    with pytest.raises(ValueError, match="block_no"):
        block_valid(0, 4)

    data = 0xC0DE
    block_c = encode_block(data, OFFSET_C)
    block_c_prime = encode_block(data, OFFSET_C_PRIME)
    assert block_valid(block_c_prime, 2, version_b=True)
    assert not block_valid(block_c, 2, version_b=True)
    assert block_valid(block_c, 2, version_b=False)
    assert not block_valid(block_c_prime, 2, version_b=False)

    with pytest.raises(ValueError, match="expected 4 words"):
        encode_group((1, 2, 3))
    with pytest.raises(ValueError, match="group must be 8 bytes"):
        group_bytes_to_words([1, 2])
    with pytest.raises(ValueError, match="expected 4 words"):
        group_words_to_bytes((1, 2, 3))

    blocks = encode_group((0xCAFE, 0x4000, 0x1234, 0x5678))
    bits = blocks_to_bits(blocks)
    assert count_valid_blocks(bits.tolist()) == 4
    assert find_groups_in_bitstream(bits.tolist())
    assert find_groups_in_bitstream(np.asarray(bits, dtype=np.int64))
    assert len(differential_decode(np.array([1], dtype=np.uint8))) == 0

    garbage = np.zeros(BLOCK_BITS - 1, dtype=np.uint8)
    assert count_valid_blocks(garbage) == 0


def test_rds_clock_invalid_inputs(monkeypatch):
    naive = datetime(2026, 5, 17, 12, 0)
    b_extra, c, d = encode_clock_time(naive)
    assert decode_clock_time((4 << 12) | b_extra, c, d).utc == naive.replace(tzinfo=UTC)

    with pytest.raises(ValueError, match="MJD out of 17-bit range"):
        encode_clock_time(datetime(2300, 1, 1, tzinfo=UTC))

    class FakeDateTime:
        tzinfo = UTC
        hour = 24
        minute = 0

        def utcoffset(self):
            return timedelta(0)

        def astimezone(self, tz):
            return self

    monkeypatch.setattr("rdsclock.rds_clock.datetime_to_mjd", lambda dt: 60_000)
    with pytest.raises(ValueError, match="hour/minute out of range"):
        encode_clock_time(FakeDateTime())

    assert decode_clock_time(-1, 0, 0) is None

    valid_mjd = 60_000
    b = (4 << 12) | ((valid_mjd >> 15) & 0x3)
    c_bad_minute = ((valid_mjd & 0x7FFF) << 1) | 0
    d_bad_minute = (12 << 12) | (60 << 6)
    assert decode_clock_time(b, c_bad_minute, d_bad_minute) is None

    monkeypatch.setattr(
        "rdsclock.rds_clock.mjd_to_date", lambda mjd: (_ for _ in ()).throw(OverflowError)
    )
    d_ok = (12 << 12) | (30 << 6)
    assert decode_clock_time(b, c_bad_minute, d_ok) is None


def test_rds_groups_radiotext_version_b_and_safe_chars():
    info = StationInfo()
    group_2a = group_words_to_bytes(
        (
            0xCAFE,
            (2 << 12) | (0 << 11) | 0,
            (0x01 << 8) | ord("A"),
            (0x0D << 8) | ord("B"),
        )
    )
    parse_group(group_2a, info)
    assert info.rt_text == " A"
    assert info.group_counts["2A"] == 1

    group_2b = group_words_to_bytes(
        (
            0xCAFE,
            (2 << 12) | (1 << 11) | (1 << 4) | 1,
            0,
            (ord("H") << 8) | ord("I"),
        )
    )
    parse_group(group_2b, info)
    assert info.rt_ab_flag == 1
    assert info.rt_text[2:4] == "HI"
    assert info.group_counts["2B"] == 1


class _MemoryReconClient:
    def __init__(self):
        self.calls: list[tuple[str, int | None]] = []

    def set_sample_rate(self, value):
        self.calls.append(("sample_rate", value))

    def set_gain_mode_auto(self):
        self.calls.append(("gain_auto", None))

    def set_gain_mode_manual(self, value):
        self.calls.append(("gain_manual", value))

    def set_frequency(self, value):
        self.calls.append(("frequency", value))

    def read_iq(self, n_samples, settle_s=0.0):
        return np.ones(max(n_samples, 1), dtype=np.complex64)


@contextlib.contextmanager
def _fake_rtl_tcp(samples: bytes):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve():
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            try:
                conn.sendall(b"RTL0" + struct.pack(">II", 5, 29))
                with contextlib.suppress(BrokenPipeError, OSError):
                    conn.sendall(samples * 8)
                conn.settimeout(0.1)
                while not stop.is_set():
                    try:
                        if not conn.recv(4096):
                            break
                    except TimeoutError:
                        continue
            finally:
                with contextlib.suppress(OSError):
                    conn.close()
        with contextlib.suppress(OSError):
            srv.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        with contextlib.suppress(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
        thread.join(timeout=0.5)


def test_recon_scan_hop_and_live_loop_branches(monkeypatch):
    client = _MemoryReconClient()
    cfg = recon.ReconConfig(
        band_start_mhz=95.5,
        band_end_mhz=95.5,
        scan_step_mhz=0.1,
        scan_dwell_s=0.001,
        dwell_s=0.001,
        sample_rate=1000,
        fs_scan=1000,
        gain_db=7.5,
    )
    monkeypatch.setattr(recon, "decode_iq", lambda iq, fs, **kwargs: _decode_result(clock=_clock()))
    progress: list[str] = []
    candidates = recon.quick_scan_band(client, cfg, progress=progress.append)
    assert candidates[0].has_ct
    assert ("gain_manual", 75) in client.calls
    assert progress

    station_ct = recon.StationCandidate(95.5e6, -10.0, 2, True, 0xCAFE, "TEST")
    station_no_ct = recon.StationCandidate(96.1e6, -12.0, 1, False, 0xBEEF, "NONE")
    calls = iter([_decode_result(clock=_clock()), _decode_result(clock=None, groups=3)])
    monkeypatch.setattr(recon, "decode_iq", lambda iq, fs, **kwargs: next(calls))
    consensus = recon.TimeConsensus()
    progress.clear()
    assert (
        recon.hop_collect_ct(client, [station_ct, station_no_ct], cfg, consensus, progress.append)
        == 1
    )
    assert any("no CT" in message for message in progress)

    samples = bytes([180, 128] * 256)
    monkeypatch.setattr(
        recon, "decode_iq", lambda iq, fs, **kwargs: _decode_result(clock=None, groups=4)
    )
    monkeypatch.setattr(recon.time, "sleep", lambda seconds: None)
    status: list[str] = []
    with _fake_rtl_tcp(samples) as port:
        live_cfg = recon.ReconConfig(
            band_start_mhz=95.5,
            band_end_mhz=95.5,
            scan_step_mhz=0.1,
            scan_dwell_s=0.001,
            dwell_s=0.001,
            sample_rate=1000,
            fs_scan=1000,
            rssi_threshold_db=-200,
            idle_s=0.01,
            iterations=2,
            host="127.0.0.1",
            port=port,
        )
        recon.run_recon(live_cfg, on_status=status.append)
    assert any("reached iterations=2" in message for message in status)


def test_recon_live_loop_keyboard_interrupt(monkeypatch):
    monkeypatch.setattr(
        recon,
        "quick_scan_band",
        lambda client, cfg, progress=None: [recon.StationCandidate(95.5e6, -10.0, 1, True, 0xCAFE)],
    )
    monkeypatch.setattr(
        recon,
        "hop_collect_ct",
        lambda client, watchlist, cfg, consensus, progress=None: (_ for _ in ()).throw(
            KeyboardInterrupt
        ),
    )
    status: list[str] = []
    with _fake_rtl_tcp(bytes([128, 128] * 64)) as port:
        cfg = recon.ReconConfig(
            band_start_mhz=95.5,
            band_end_mhz=95.5,
            scan_dwell_s=0.001,
            dwell_s=0.001,
            iterations=1,
            host="127.0.0.1",
            port=port,
        )
        recon.run_recon(cfg, on_status=status.append)
    assert any("Interrupted" in message for message in status)


def test_recon_offline_error_no_ct_limit_and_outlier(tmp_path, monkeypatch):
    for name in [
        "bad_name.iq",
        "fm_092.00_MHz.iq",
        "fm_093.00_MHz.iq",
        "fm_098.30_MHz.iq",
        "fm_106.80_MHz.iq",
        "fm_107.90_MHz.iq",
    ]:
        (tmp_path / name).touch()

    base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)

    def fake_decode_file(path, fs, progress=None):
        name = str(path)
        if "098.30" in name:
            raise ValueError("bad capture")
        if "106.80" in name:
            return _decode_result(clock=None, groups=2, ps_name="NOCT")
        if "107.90" in name:
            return _decode_result(clock=ClockTime(base + timedelta(minutes=10), 0), groups=2)
        if "093.00" in name:
            return _decode_result(clock=ClockTime(base + timedelta(seconds=2), 0), groups=2)
        return _decode_result(clock=ClockTime(base, 0), groups=2)

    monkeypatch.setattr(recon, "decode_file", fake_decode_file)
    statuses: list[str] = []
    consensus = recon.run_recon_offline(
        recon.ReconConfig(mission_precision_s=60.0),
        str(tmp_path),
        on_status=statuses.append,
    )
    result = consensus.consensus()
    assert result.utc is not None
    assert result.outlier_freqs_mhz
    assert any("skipping bad_name" in message for message in statuses)
    assert any("ValueError" in message for message in statuses)
    assert any("no CT" in message for message in statuses)
    assert any("OUTLIERS" in message for message in statuses)

    calls: list[str] = []

    def recording_decode_file(path, fs, progress=None):
        calls.append(str(path))
        return _decode_result(clock=None, groups=0)

    monkeypatch.setattr(recon, "decode_file", recording_decode_file)
    recon.run_recon_offline(
        recon.ReconConfig(),
        str(tmp_path),
        on_status=lambda message: None,
        limit_files=2,
    )
    assert len(calls) == 1


def test_rtl_tcp_error_and_reconnect_branches():
    class ClosingSocket:
        def recv(self, n):
            return b""

    with pytest.raises(OSError, match="socket closed"):
        RtlTcpClient._recv_exact(ClosingSocket(), 1)

    client = RtlTcpClient()
    with pytest.raises(RuntimeError, match="not connected"):
        client.set_frequency(95_500_000)
    with pytest.raises(RuntimeError, match="not connected"):
        client.read_iq(1, settle_s=0.0)

    bad_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    bad_server.bind(("127.0.0.1", 0))
    bad_server.listen(1)
    port = bad_server.getsockname()[1]

    def serve_bad_magic():
        conn, _ = bad_server.accept()
        conn.sendall(b"BAD!" + struct.pack(">II", 0, 0))
        conn.close()
        bad_server.close()

    thread = threading.Thread(target=serve_bad_magic, daemon=True)
    thread.start()
    with pytest.raises(OSError, match="unexpected magic"):
        RtlTcpClient(host="127.0.0.1", port=port).connect()
    thread.join(timeout=0.5)

    good_client = RtlTcpClient()
    good_client.info = SimpleNamespace(magic=b"RTL0", tuner_type=1, gain_count=2)
    good_client._sock = object()
    assert good_client.connect() is good_client.info


def test_synth_validation_audio_resample_and_noise(monkeypatch):
    group = group_words_to_bytes((0xCAFE, 0x4000, 0x1234, 0x5678))
    with pytest.raises(ValueError, match="version_b_per_group"):
        rds_groups_to_bits([group], version_b_per_group=[True, False])
    assert len(rds_groups_to_bits([group], version_b_per_group=[True], differential=False)) == 104

    with pytest.raises(ValueError, match="integer multiple"):
        rds_baseband(np.array([0, 1], dtype=np.uint8), fs=1000, symbol_rate=333)

    rds_signal = np.ones(8, dtype=np.float32)
    short_audio_mpx = make_mpx(rds_signal, fs=DEFAULT_INTERMEDIATE_FS, audio=np.ones(4))
    assert len(short_audio_mpx) == 8
    assert float(np.min(short_audio_mpx[:4])) > 0.3
    assert len(make_mpx(rds_signal, fs=DEFAULT_INTERMEDIATE_FS, audio=np.ones(16))) == 8

    mpx = np.full(16, 2.0, dtype=np.float32)
    iq = fm_modulate(mpx, fs=DEFAULT_INTERMEDIATE_FS, carrier_offset_hz=100)
    assert iq.dtype == np.complex64

    resampled = resample_to(np.ones(10, dtype=np.complex64), from_fs=10, to_fs=20)
    assert len(resampled) == 20

    zeros = np.zeros(8, dtype=np.complex64)
    np.testing.assert_array_equal(add_awgn(zeros, snr_db=10), zeros)
    monkeypatch.setattr(
        "rdsclock.synth._nondeterministic_noise_rng", lambda: np.random.default_rng(123)
    )
    noisy = add_awgn(np.ones(16, dtype=np.complex64), snr_db=30, rng=None)
    assert noisy.dtype == np.complex64

    with pytest.raises(ValueError, match="groups must be non-empty"):
        synthesize_fm_iq([], duration_s=0.01)
    with pytest.raises(ValueError, match="duration_s"):
        synthesize_fm_iq([group], duration_s=0)

    tone_iq = synthesize_fm_iq(
        [group],
        duration_s=0.01,
        fs=DEFAULT_INTERMEDIATE_FS,
        snr_db=None,
        audio_tone_hz=1000,
    )
    assert tone_iq.dtype == np.complex64
