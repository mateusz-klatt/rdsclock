import argparse
import builtins
import runpy
import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import numpy as np
import pytest

from rdsclock import audio, cli, decoder, dsp, plot, recon, synth, time_consensus
from rdsclock.channelizer import auto_center, decode_channels, extract_channel, required_bandwidth
from rdsclock.decoder import DecodeResult
from rdsclock.rds_blocks import (
    OFFSET_A,
    OFFSET_C,
    OFFSET_C_PRIME,
    block_valid,
    blocks_to_bits,
    count_valid_blocks,
    differential_decode,
    encode_block,
    group_words_to_bytes,
)
from rdsclock.rds_clock import ClockTime, encode_clock_time
from rdsclock.rds_groups import StationInfo, parse_group
from rdsclock.recon import ReconConfig, StationCandidate
from rdsclock.rtl_tcp import DongleInfo, RtlTcpClient
from rdsclock.time_consensus import StationTrack, TimeConsensus


def _clock_time(hour: int = 12, minute: int = 0, offset_minutes: int = 0) -> ClockTime:
    return ClockTime(
        utc=datetime(2026, 5, 17, hour, minute, tzinfo=UTC),
        local_offset_minutes=offset_minutes,
    )


def _station_info(
    *,
    pi: int = 0xCAFE,
    ps_name: str = "TEST",
    clock: ClockTime | None = None,
) -> StationInfo:
    info = StationInfo(pi=pi)
    info.ps_chars = list(ps_name.ljust(8)[:8])
    if clock is not None:
        info.clock_times.append(clock)
    return info


def _decode_result(
    *,
    n_groups: int = 1,
    ps_name: str = "TEST",
    clock: ClockTime | None = None,
    pi: int = 0xCAFE,
    freq_offset_hz: float = 0.0,
) -> DecodeResult:
    return DecodeResult(
        info=_station_info(pi=pi, ps_name=ps_name, clock=clock),
        n_groups=n_groups,
        n_bits=max(n_groups, 1) * 10,
        freq_offset_hz=freq_offset_hz,
        symbol_offset=0,
    )


def _sounddevice_module(
    *, stream: Mock | None = None, play: Mock | None = None, stop: Mock | None = None
):
    module = ModuleType("sounddevice")
    module.default = SimpleNamespace(samplerate=None, channels=None)
    module.OutputStream = MagicMock()
    module.OutputStream.return_value.__enter__.return_value = stream or Mock()
    module.OutputStream.return_value.__exit__.return_value = False
    module.play = play or Mock()
    module.stop = stop or Mock()
    return module


def _import_without_sounddevice(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "sounddevice":
        raise ImportError("sounddevice unavailable")
    return _REAL_IMPORT(name, globals, locals, fromlist, level)


def _import_without_matplotlib(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "matplotlib":
        raise ImportError("matplotlib unavailable")
    return _REAL_IMPORT(name, globals, locals, fromlist, level)


_REAL_IMPORT = builtins.__import__


class TestAudioPlaybackCoverage:
    def test_play_iq_live_reports_missing_sounddevice(self):
        with (
            patch("builtins.__import__", side_effect=_import_without_sounddevice),
            pytest.raises(RuntimeError, match="sounddevice"),
        ):
            audio.play_iq_live(freq_mhz=99.9)

    @pytest.mark.parametrize("gain_db", [None, 12.5])
    def test_play_iq_live_uses_mock_stream_and_client(self, gain_db):
        stream = Mock()
        stream.write.side_effect = KeyboardInterrupt()
        sd_module = _sounddevice_module(stream=stream)

        fake_client = Mock()
        fake_client.info = SimpleNamespace(tuner_type=5, gain_count=29)
        fake_iq = np.exp(1j * 2 * np.pi * 1_000 * np.arange(4_800) / audio.DEFAULT_LIVE_FS).astype(
            np.complex64
        )
        fake_client.read_iq.side_effect = [fake_iq, fake_iq]
        client_cm = MagicMock()
        client_cm.__enter__.return_value = fake_client
        client_cm.__exit__.return_value = False

        with (
            patch.dict(sys.modules, {"sounddevice": sd_module}),
            patch("rdsclock.audio.RtlTcpClient", return_value=client_cm),
        ):
            audio.play_iq_live(freq_mhz=99.9, host="127.0.0.1", port=1234, gain_db=gain_db)

        assert sd_module.default.samplerate == audio.DEFAULT_AUDIO_RATE
        assert sd_module.default.channels == 1
        fake_client.set_sample_rate.assert_called_once_with(audio.DEFAULT_LIVE_FS)
        fake_client.set_frequency.assert_called_once_with(int(99.9 * 1e6))
        if gain_db is None:
            fake_client.set_gain_mode_auto.assert_called_once()
            fake_client.set_gain_mode_manual.assert_not_called()
        else:
            fake_client.set_gain_mode_manual.assert_called_once_with(int(gain_db * 10))
        stream.write.assert_called_once()

    def test_play_iq_file_reports_missing_sounddevice(self):
        with (
            patch("builtins.__import__", side_effect=_import_without_sounddevice),
            pytest.raises(RuntimeError, match="sounddevice"),
        ):
            audio.play_iq_file("recording.iq")

    def test_play_iq_file_plays_complex64_input(self):
        sd_module = _sounddevice_module()
        iq = np.exp(1j * 2 * np.pi * 1_000 * np.arange(2_500) / 250_000).astype(np.complex64)
        rendered = np.ones(100, dtype=np.float32)
        with (
            patch.dict(sys.modules, {"sounddevice": sd_module}),
            patch("rdsclock.dsp.read_iq_complex64", return_value=iq),
            patch("rdsclock.audio.fm_audio_from_iq", return_value=rendered) as fm_audio_from_iq,
        ):
            audio.play_iq_file("recording.iq", fs_in=240_000, fs_audio=48_000)

        fm_audio_from_iq.assert_called_once_with(iq, fs_in=240_000, fs_out=48_000)
        sd_module.play.assert_called_once_with(rendered, samplerate=48_000, blocking=True)

    def test_play_iq_file_falls_back_to_u8_and_stops_on_interrupt(self):
        sd_play = Mock(side_effect=KeyboardInterrupt())
        sd_stop = Mock()
        sd_module = _sounddevice_module(play=sd_play, stop=sd_stop)
        iq = np.ones(64, dtype=np.complex64)
        rendered = np.ones(20, dtype=np.float32)
        with (
            patch.dict(sys.modules, {"sounddevice": sd_module}),
            patch("rdsclock.dsp.read_iq_complex64", return_value=np.zeros(0, dtype=np.complex64)),
            patch("rdsclock.dsp.read_iq_u8", return_value=iq),
            patch("rdsclock.audio.fm_audio_from_iq", return_value=rendered) as fm_audio_from_iq,
        ):
            audio.play_iq_file("recording.iq", fs_in=250_000, fs_audio=48_000)

        fm_audio_from_iq.assert_called_once_with(iq, fs_in=250_000, fs_out=2_000)
        sd_stop.assert_called_once()


class TestDecoderCoverage:
    def test_decode_result_clock_times_property(self):
        clock = _clock_time()
        result = DecodeResult(
            info=_station_info(clock=clock),
            n_groups=1,
            n_bits=10,
            freq_offset_hz=0.0,
            symbol_offset=0,
        )
        assert result.clock_times == [clock]

    def test_decode_iq_uses_default_carrier_when_auto_disabled(self, monkeypatch):
        monkeypatch.setattr(decoder.dsp, "channel_filter", lambda iq, fs: iq)
        monkeypatch.setattr(decoder.dsp, "fm_demod", lambda iq: np.ones(64, dtype=np.float32))
        carrier_seen = {}

        def fake_shift_and_filter(baseband, fs, carrier):
            carrier_seen["value"] = carrier
            return np.ones(64, dtype=np.complex64)

        monkeypatch.setattr(decoder.dsp, "shift_and_filter", fake_shift_and_filter)
        monkeypatch.setattr(decoder.dsp, "decimate_to_rds_rate", lambda data, input_fs: data)
        monkeypatch.setattr(decoder.dsp, "coarse_freq_correction", lambda data, fs: (data, 0.0))
        monkeypatch.setattr(decoder.dsp, "symbol_lpf", lambda data, fs, cutoff: data)
        monkeypatch.setattr(decoder.dsp, "costas_loop_bpsk", lambda data, alpha, beta: data)
        monkeypatch.setattr(decoder.dsp, "best_symbol_offset", lambda data, sps: (data, 0))
        monkeypatch.setattr(
            decoder.dsp, "bits_from_symbols_diff", lambda data: np.zeros(0, dtype=np.uint8)
        )
        monkeypatch.setattr(
            decoder.dsp, "clock_recovery_mm", lambda data, sps: np.zeros(0, dtype=np.complex64)
        )
        monkeypatch.setattr(decoder, "_best_variant_groups", lambda bits: ([], "normal"))
        monkeypatch.setattr(decoder, "parse_groups", lambda groups: StationInfo())

        decoder.decode_iq(np.ones(65, dtype=np.complex64), auto_carrier=False)
        assert carrier_seen["value"] == dsp.RDS_CARRIER_HZ

    def test_decode_iq_short_stream_fallback_can_select_mm(self, monkeypatch):
        monkeypatch.setattr(decoder.dsp, "channel_filter", lambda iq, fs: iq)
        monkeypatch.setattr(decoder.dsp, "fm_demod", lambda iq: np.ones(64, dtype=np.float32))
        monkeypatch.setattr(
            decoder.dsp,
            "shift_and_filter",
            lambda baseband, fs, carrier: np.ones(64, dtype=np.complex64),
        )
        monkeypatch.setattr(decoder.dsp, "decimate_to_rds_rate", lambda data, input_fs: data)
        monkeypatch.setattr(decoder.dsp, "coarse_freq_correction", lambda data, fs: (data, 0.0))
        monkeypatch.setattr(decoder.dsp, "symbol_lpf", lambda data, fs, cutoff: data)
        monkeypatch.setattr(decoder.dsp, "costas_loop_bpsk", lambda data, alpha, beta: data)
        monkeypatch.setattr(
            decoder.dsp,
            "best_symbol_offset",
            lambda data, sps: (np.array([1, -1], dtype=np.complex64), 3),
        )
        monkeypatch.setattr(
            decoder.dsp,
            "clock_recovery_mm",
            lambda data, sps: np.array([1, -1, 1], dtype=np.complex64),
        )
        variants = iter(
            [
                ([], "bo"),
                ([bytearray(b"\x00" * 8)], "mm"),
            ]
        )
        monkeypatch.setattr(decoder, "_best_variant_groups", lambda bits: next(variants))
        monkeypatch.setattr(decoder, "parse_groups", lambda groups: StationInfo())

        result = decoder.decode_iq(np.ones(65, dtype=np.complex64), auto_carrier=False)

        assert result.n_groups == 1
        assert result.symbol_offset == -1

    def test_decode_file_autodetects_u8_when_complex_sniff_is_invalid(self, monkeypatch):
        monkeypatch.setattr(decoder.os.path, "getsize", lambda path: 16)
        monkeypatch.setattr(
            decoder.np, "fromfile", lambda path, dtype, count: np.zeros(256, dtype=np.complex64)
        )
        u8_iq = np.ones(8, dtype=np.complex64)
        expected = _decode_result()
        monkeypatch.setattr(decoder.dsp, "read_iq_u8", lambda path: u8_iq)
        monkeypatch.setattr(decoder, "decode_iq", lambda iq, fs, progress: expected)
        result = decoder.decode_file("capture.iq")
        assert result is expected

    def test_decode_file_rejects_unknown_format(self, monkeypatch):
        monkeypatch.setattr(decoder.os.path, "getsize", lambda path: 0)
        with pytest.raises(ValueError, match="unknown IQ format"):
            decoder.decode_file("capture.iq", fmt="bogus")

    def test_decode_file_rejects_empty_iq(self, monkeypatch):
        monkeypatch.setattr(decoder.os.path, "getsize", lambda path: 0)
        monkeypatch.setattr(
            decoder.dsp, "read_iq_complex64", lambda path: np.zeros(0, dtype=np.complex64)
        )
        with pytest.raises(ValueError, match="empty IQ file"):
            decoder.decode_file("capture.iq", fmt="complex64")


class TestDspCoverage:
    def test_read_iq_u8_discards_odd_trailing_byte(self, tmp_path):
        path = tmp_path / "odd.iq"
        np.array([127, 129, 128], dtype=np.uint8).tofile(path)
        iq = dsp.read_iq_u8(str(path))
        assert len(iq) == 1

    def test_estimate_pilot_returns_default_for_short_or_out_of_band_input(self):
        short = dsp.estimate_pilot_19khz(np.ones(512, dtype=np.float32), fs=250_000)
        low_fs = dsp.estimate_pilot_19khz(np.ones(2_048, dtype=np.float32), fs=1_000)
        assert short == 19_000.0
        assert low_fs == 19_000.0

    def test_estimate_rds_carrier_fft_fallback_and_short_input(self):
        fs = 250_000
        n = np.arange(4_096)
        tone = np.cos(2 * np.pi * 57_250 * n / fs).astype(np.float32)
        carrier = dsp.estimate_rds_carrier(tone, fs=fs, use_pilot=False)
        assert carrier == pytest.approx(57_250, abs=150)
        assert (
            dsp.estimate_rds_carrier(np.ones(128, dtype=np.float32), fs=fs, use_pilot=False)
            == dsp.RDS_CARRIER_HZ
        )

    def test_misc_short_input_paths(self):
        corrected, freq = dsp.coarse_freq_correction(
            np.array([1 + 0j], dtype=np.complex64), fs=19_000
        )
        assert freq == 0.0
        np.testing.assert_array_equal(corrected, np.array([1 + 0j], dtype=np.complex64))

        filtered = dsp.symbol_lpf(np.ones(8, dtype=np.complex64), fs=19_000)
        assert len(filtered) == 8
        assert dsp.clock_recovery_mm(np.ones(4, dtype=np.complex64), sps=16).size == 0
        assert dsp.bits_from_symbols_diff(np.array([1 + 0j], dtype=np.complex64)).size == 0

    def test_decimate_to_rds_rate_generic_path(self):
        data = np.ones(1_000, dtype=np.complex64)
        decimated = dsp.decimate_to_rds_rate(data, input_fs=228_000)
        assert decimated.dtype == np.complex64
        assert len(decimated) > 0


class TestRdsBlockCoverage:
    def test_validation_and_error_paths(self):
        with pytest.raises(ValueError, match="dataword out of range"):
            encode_block(1 << 16, OFFSET_A)
        block = encode_block(0x1234, OFFSET_C_PRIME)
        assert block_valid(block, 2, version_b=True)
        with pytest.raises(ValueError, match="block_no"):
            block_valid(block, 4)

    def test_count_valid_blocks_and_conversion_paths(self):
        blocks = [encode_block(0x1234, OFFSET_A), encode_block(0x5678, OFFSET_C)]
        bits = blocks_to_bits(blocks)
        as_list = bits.tolist()
        assert count_valid_blocks(as_list) == 2
        assert count_valid_blocks(bits.astype(np.int64)) == 2
        assert differential_decode(np.array([1], dtype=np.uint8)).size == 0

    def test_group_helpers_reject_bad_lengths(self):
        from rdsclock.rds_blocks import encode_group, group_bytes_to_words, group_words_to_bytes

        with pytest.raises(ValueError, match="expected 4 words"):
            encode_group((1, 2, 3))
        with pytest.raises(ValueError, match="group must be 8 bytes"):
            group_bytes_to_words(bytearray(7))
        with pytest.raises(ValueError, match="expected 4 words"):
            group_words_to_bytes((1, 2, 3))


class TestRdsClockCoverage:
    def test_encode_clock_time_handles_naive_datetime_and_future_range_check(self):
        extra, c, d = encode_clock_time(datetime(2026, 5, 17, 12, 0))
        restored = __import__(
            "rdsclock.rds_clock", fromlist=["decode_clock_time"]
        ).decode_clock_time((4 << 12) | (extra & 0x3), c, d)
        assert restored is not None
        assert restored.local_offset_minutes == 0

        with pytest.raises(ValueError, match="MJD out of 17-bit range"):
            encode_clock_time(datetime(2500, 1, 1, 0, 0, tzinfo=UTC), local_offset_minutes=0)

    def test_decode_clock_time_rejects_out_of_range_block_values(self):
        from rdsclock.rds_clock import decode_clock_time

        assert decode_clock_time(-1, 0, 0) is None


class TestRdsGroupsCoverage:
    def test_parse_group_2a_handles_control_and_non_printable_chars(self):
        info = StationInfo()
        group = group_words_to_bytes(
            (
                0xCAFE,
                (2 << 12),
                (ord("A") << 8) | 0x0D,
                (1 << 8) | ord("B"),
            )
        )
        parse_group(group, info)
        assert info.rt_text == "A"
        assert info.rt_chars[2] == " "
        assert info.rt_chars[3] == "B"

    def test_parse_group_2b_resets_text_on_ab_flag_change(self):
        info = StationInfo()
        first = group_words_to_bytes(
            (0xCAFE, (2 << 12) | (1 << 11) | 1, 0, (ord("H") << 8) | ord("I"))
        )
        second = group_words_to_bytes(
            (0xCAFE, (2 << 12) | (1 << 11) | (1 << 4), 0, (ord("O") << 8) | 1)
        )
        parse_group(first, info)
        parse_group(second, info)
        assert info.rt_chars[0] == "O"
        assert info.rt_chars[1] == " "
        assert info.group_counts["2B"] == 2


class TestSynthCoverage:
    def test_validation_and_audio_branches(self):
        groups = [group_words_to_bytes((0xCAFE, 0x4000, 0x1234, 0x5678))]
        with pytest.raises(ValueError, match="version_b_per_group length"):
            synth.rds_groups_to_bits(groups, version_b_per_group=[])
        with pytest.raises(ValueError, match="integer multiple"):
            synth.rds_baseband(np.array([0, 1], dtype=np.uint8), fs=200_000)
        with pytest.raises(ValueError, match="groups must be non-empty"):
            synth.synthesize_fm_iq([], duration_s=1.0)
        with pytest.raises(ValueError, match="duration_s must be > 0"):
            synth.synthesize_fm_iq(groups, duration_s=0.0)

        iq = synth.synthesize_fm_iq(
            groups,
            duration_s=0.05,
            snr_db=None,
            audio_tone_hz=1_000.0,
            rng=np.random.default_rng(0),
        )
        assert len(iq) > 0

    def test_make_mpx_pad_truncate_and_fm_normalisation(self):
        rds = np.ones(32, dtype=np.float32)
        padded = synth.make_mpx(
            rds, fs=synth.DEFAULT_INTERMEDIATE_FS, audio=np.ones(8, dtype=np.float32)
        )
        trimmed = synth.make_mpx(
            rds, fs=synth.DEFAULT_INTERMEDIATE_FS, audio=np.ones(64, dtype=np.float32)
        )
        assert len(padded) == len(rds)
        assert len(trimmed) == len(rds)

        iq = synth.fm_modulate(np.full(32, 2.0, dtype=np.float32), fs=synth.DEFAULT_INTERMEDIATE_FS)
        assert np.allclose(np.abs(iq), 1.0, atol=1e-5)

    def test_add_awgn_zero_signal_and_internal_rng(self, monkeypatch):
        zero = np.zeros(8, dtype=np.complex64)
        copy = synth.add_awgn(zero, snr_db=10.0)
        np.testing.assert_array_equal(copy, zero)

        fake_rng = Mock()
        fake_rng.standard_normal.side_effect = [np.zeros(8), np.zeros(8)]
        default_rng = Mock(return_value=fake_rng)
        monkeypatch.setattr(synth.np.random, "default_rng", default_rng)
        noisy = synth.add_awgn(np.ones(8, dtype=np.complex64), snr_db=10.0, rng=None)
        assert noisy.dtype == np.complex64
        default_rng.assert_called_once()


class TestReconCoverage:
    @pytest.mark.parametrize(
        ("gain_db", "expected_method"),
        [(None, "set_gain_mode_auto"), (15.0, "set_gain_mode_manual")],
    )
    def test_quick_scan_band_emits_progress_and_handles_gain(
        self, monkeypatch, gain_db, expected_method
    ):
        fake_client = Mock()
        fake_client.read_iq.return_value = np.ones(64, dtype=np.complex64)
        progress = []
        cfg = ReconConfig(
            band_start_mhz=90.0,
            band_end_mhz=90.0,
            scan_step_mhz=0.1,
            scan_dwell_s=0.01,
            rssi_threshold_db=-100.0,
            gain_db=gain_db,
        )
        monkeypatch.setattr(
            recon,
            "decode_iq",
            lambda iq, fs: _decode_result(n_groups=3, ps_name="SCAN", clock=None, pi=0x1234),
        )

        candidates = recon.quick_scan_band(fake_client, cfg, progress=progress.append)
        assert len(candidates) == 1
        getattr(fake_client, expected_method).assert_called()
        assert progress and "scan  90.00 MHz" in progress[0]

    def test_hop_collect_ct_covers_ct_and_no_ct_paths(self, monkeypatch):
        fake_client = Mock()
        fake_client.read_iq.return_value = np.ones(64, dtype=np.complex64)
        progress = []
        now = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
        results = iter(
            [
                _decode_result(
                    n_groups=4,
                    ps_name="YES",
                    clock=ClockTime(now, 0),
                    pi=0x1001,
                    freq_offset_hz=12.0,
                ),
                _decode_result(n_groups=1, ps_name="NO", clock=None, pi=0x1002),
            ]
        )
        monkeypatch.setattr(recon, "decode_iq", lambda iq, fs: next(results))
        consensus = TimeConsensus()
        watchlist = [
            StationCandidate(freq_hz=90.0e6, rssi_db=-10.0, n_groups=4, has_ct=True, pi=0x1001),
            StationCandidate(freq_hz=91.0e6, rssi_db=-20.0, n_groups=1, has_ct=False, pi=0x1002),
        ]
        cfg = ReconConfig(sample_rate=250_000, dwell_s=0.01)
        observed = recon.hop_collect_ct(
            fake_client, watchlist, cfg, consensus, progress=progress.append
        )
        assert observed == 1
        assert len(consensus.tracks) == 1
        assert any("no CT" in line for line in progress)

    def test_render_status_renders_outliers(self):
        class DummyConsensus:
            def consensus(self):
                return SimpleNamespace(
                    utc=None,
                    outlier_freqs_mhz=[92.0],
                    format_display=lambda: "TIME UNAVAILABLE",
                )

            def summary(self):
                return "summary"

        text = recon.render_status(
            DummyConsensus(), [], next_rescan_in_s=5.0, system_now=datetime.now(UTC)
        )
        assert "OUTLIERS:  92.00 MHz" in text

    def test_run_recon_uses_fake_rtl_tcp_and_fallback_watchlist(self, fake_rtl_tcp, monkeypatch):
        statuses = []
        monkeypatch.setattr(
            recon,
            "quick_scan_band",
            lambda client, cfg, progress=None: [
                StationCandidate(
                    freq_hz=90.0e6, rssi_db=-10.0, n_groups=2, has_ct=False, pi=0x1001
                ),
                StationCandidate(
                    freq_hz=91.0e6, rssi_db=-11.0, n_groups=1, has_ct=False, pi=0x1002
                ),
            ],
        )
        monkeypatch.setattr(
            recon, "hop_collect_ct", lambda client, watchlist, cfg, consensus, progress=None: 0
        )
        monkeypatch.setattr(
            recon, "render_status", lambda consensus, watchlist, next_rescan_in: "STATUS"
        )
        cfg = ReconConfig(
            band_start_mhz=90.0,
            band_end_mhz=90.0,
            scan_step_mhz=0.1,
            scan_dwell_s=0.01,
            dwell_s=0.01,
            idle_s=0.0,
            iterations=1,
            host="127.0.0.1",
            port=fake_rtl_tcp,
        )
        recon.run_recon(cfg, on_status=statuses.append)
        assert any("watchlist: ['90.00', '91.00']" in line for line in statuses)
        assert any("reached iterations=1" in line for line in statuses)

    def test_run_recon_handles_keyboard_interrupt(self, fake_rtl_tcp, monkeypatch):
        statuses = []
        monkeypatch.setattr(
            recon,
            "quick_scan_band",
            lambda client, cfg, progress=None: [
                StationCandidate(freq_hz=90.0e6, rssi_db=-10.0, n_groups=2, has_ct=True, pi=0x1001)
            ],
        )
        monkeypatch.setattr(
            recon, "hop_collect_ct", lambda client, watchlist, cfg, consensus, progress=None: 1
        )
        monkeypatch.setattr(
            recon, "render_status", lambda consensus, watchlist, next_rescan_in: "STATUS"
        )
        monkeypatch.setattr(recon.time, "sleep", Mock(side_effect=KeyboardInterrupt()))
        cfg = ReconConfig(
            band_start_mhz=90.0,
            band_end_mhz=90.0,
            scan_step_mhz=0.1,
            scan_dwell_s=0.01,
            dwell_s=0.01,
            idle_s=0.1,
            iterations=None,
            host="127.0.0.1",
            port=fake_rtl_tcp,
        )
        recon.run_recon(cfg, on_status=statuses.append)
        assert any("Interrupted by operator" in line for line in statuses)

    def test_run_recon_offline_covers_skip_error_no_ct_and_outliers(self, tmp_path, monkeypatch):
        (tmp_path / "badname.iq").write_bytes(b"x")
        (tmp_path / "fm_090.00_MHz.iq").write_bytes(b"x")
        (tmp_path / "fm_091.00_MHz.iq").write_bytes(b"x")
        (tmp_path / "fm_092.00_MHz.iq").write_bytes(b"x")
        statuses = []

        class FakeConsensus:
            def __init__(self, *args, **kwargs):
                self.observations = []

            def record(self, obs):
                self.observations.append(obs)

            def consensus(self, monotonic_now=None):
                return SimpleNamespace(
                    outlier_freqs_mhz=[106.8],
                    format_display=lambda: "UTC 2026-05-17 12:00:00  ±1s",
                )

            def summary(self, monotonic_now=None):
                return "summary"

        def fake_decode_file(path, fs, progress=None):
            name = path.rsplit("/", 1)[-1]
            if name.startswith("fm_090"):
                raise ValueError("bad capture")
            if name.startswith("fm_091"):
                return _decode_result(n_groups=2, ps_name="NOCT", clock=None, pi=0x1001)
            return _decode_result(n_groups=4, ps_name="GOOD", clock=_clock_time(), pi=0x1002)

        monkeypatch.setattr(recon, "TimeConsensus", FakeConsensus)
        monkeypatch.setattr(recon, "decode_file", fake_decode_file)
        cfg = ReconConfig(sample_rate=250_000)
        recon.run_recon_offline(cfg, str(tmp_path), on_status=statuses.append, limit_files=4)
        joined = "\n".join(statuses)
        assert "cannot parse MHz" in joined
        assert "ValueError: bad capture" in joined
        assert "no CT" in joined
        assert "OUTLIERS: [106.8]" in joined


class TestCliCoverage:
    def test_scan_mark_labels(self):
        assert cli._scan_mark(_decode_result(clock=_clock_time())) == "[CT]"
        assert cli._scan_mark(_decode_result(n_groups=2, clock=None)) == "[FM]"

    def test_cmd_decode_falls_back_to_u8_reader(self, monkeypatch):
        args = argparse.Namespace(verbose=False, carrier_hz=57_000.0, file="capture.iq", fs=250_000)
        monkeypatch.setattr(
            cli.dsp, "read_iq_complex64", lambda path: np.zeros(0, dtype=np.complex64)
        )
        monkeypatch.setattr(cli.dsp, "read_iq_u8", lambda path: np.ones(8, dtype=np.complex64))
        monkeypatch.setattr(
            cli, "decode_iq", lambda iq, fs, carrier_hz, auto_carrier, progress: _decode_result()
        )
        assert cli.cmd_decode(args) == 0

    def test_cmd_scan_collects_ct_results_with_auto_gain(self, monkeypatch, capsys):
        fake_client = Mock()
        fake_client.__enter__ = Mock(return_value=fake_client)
        fake_client.__exit__ = Mock(return_value=False)
        results = iter(
            [_decode_result(clock=_clock_time()), _decode_result(clock=None, n_groups=0)]
        )
        monkeypatch.setattr(cli, "RtlTcpClient", Mock(return_value=fake_client))
        monkeypatch.setattr(cli, "decode_iq", lambda iq, fs: next(results))
        monkeypatch.setattr(
            cli,
            "_scan_one_frequency",
            lambda client, freq_mhz, args: (
                f"[CT] {freq_mhz:.2f} MHz" if freq_mhz < 90.05 else "[--] 90.10 MHz"
            ),
        )
        args = argparse.Namespace(
            start=90.0,
            end=90.1,
            step=0.1,
            duration=0.1,
            fs=250_000,
            host="127.0.0.1",
            port=1234,
            gain=None,
        )
        assert cli.cmd_scan(args) == 0
        out = capsys.readouterr().out
        fake_client.set_gain_mode_auto.assert_called_once()
        assert "Stations with Clock-Time" in out
        assert "[CT] 90.00 MHz" in out

    def test_cmd_scan_reports_when_no_station_has_ct(self, monkeypatch, capsys):
        fake_client = Mock()
        fake_client.__enter__ = Mock(return_value=fake_client)
        fake_client.__exit__ = Mock(return_value=False)
        monkeypatch.setattr(cli, "RtlTcpClient", Mock(return_value=fake_client))
        monkeypatch.setattr(cli, "_scan_one_frequency", lambda client, freq_mhz, args: "[--] none")
        args = argparse.Namespace(
            start=90.0,
            end=90.0,
            step=0.1,
            duration=0.1,
            fs=250_000,
            host="127.0.0.1",
            port=1234,
            gain=1.0,
        )
        assert cli.cmd_scan(args) == 0
        assert (
            "No station in the scanned band transmitted a valid Clock-Time."
            in capsys.readouterr().out
        )

    def test_multi_wide_saves_capture(self, monkeypatch):
        fake_client = Mock()
        fake_client.info = SimpleNamespace(tuner_type=5, gain_count=29)
        fake_client.record.return_value = np.ones(8, dtype=np.complex64)
        client_cm = MagicMock()
        client_cm.__enter__.return_value = fake_client
        client_cm.__exit__.return_value = False
        monkeypatch.setattr(cli, "RtlTcpClient", Mock(return_value=client_cm))
        monkeypatch.setattr(cli, "decode_channels", lambda **kwargs: [])
        writer = Mock()
        monkeypatch.setattr(cli.dsp, "write_iq_complex64", writer)
        args = argparse.Namespace(
            fs=2_400_000,
            host="127.0.0.1",
            port=1234,
            gain=None,
            duration=0.1,
            save="wide.iq",
            verbose=False,
        )
        assert cli._multi_wide([95.5], 95.5e6, args) == 0
        writer.assert_called_once()

    def test_cmd_multi_covers_auto_mode_and_empty_frequencies(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_multi_wide", lambda freqs, center_hz, args: 11)
        monkeypatch.setattr(cli, "_multi_hop", lambda freqs, args: 22)
        monkeypatch.setattr(cli, "fits_in_window", lambda freqs_hz, fs: True)
        args = argparse.Namespace(freqs="95.5", center=None, fs=2_400_000, mode="auto")
        assert cli.cmd_multi(args) == 11

        monkeypatch.setattr(cli, "fits_in_window", lambda freqs_hz, fs: False)
        assert cli.cmd_multi(args) == 22

        class EmptyFreqs:
            def split(self, sep):
                return []

        empty_args = argparse.Namespace(freqs=EmptyFreqs(), center=None, fs=2_400_000, mode="auto")
        assert cli.cmd_multi(empty_args) == 2
        assert "Provide at least one frequency" in capsys.readouterr().out

    def test_demo_helpers_cover_default_now_and_failure_row(self, capsys):
        ts = cli._demo_resolve_now(None)
        assert ts.tzinfo is UTC
        assert ts.second == 0
        assert cli._demo_print_row("Demo", datetime(2026, 5, 17, 12, 0, tzinfo=UTC), None) is False
        assert "FAIL" in capsys.readouterr().out

    def test_cmd_plot_falls_back_to_u8_and_returns_error_code(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli.dsp, "read_iq_complex64", lambda path: np.full(8, 200 + 0j, dtype=np.complex64)
        )
        monkeypatch.setattr(cli.dsp, "read_iq_u8", lambda path: np.ones(8, dtype=np.complex64))
        monkeypatch.setattr(cli, "plot_mpx_spectrum", Mock(side_effect=RuntimeError("no plotting")))
        args = argparse.Namespace(file="capture.iq", fs=250_000, title=None, out=None, kind="mpx")
        assert cli.cmd_plot(args) == 2
        assert "error: no plotting" in capsys.readouterr().err

    def test_cmd_play_covers_file_live_and_error_paths(self, monkeypatch, capsys):
        play_file = Mock()
        play_live = Mock()
        monkeypatch.setattr(cli, "play_iq_file", play_file)
        monkeypatch.setattr(cli, "play_iq_live", play_live)
        file_args = argparse.Namespace(
            file="capture.iq",
            fs=250_000,
            audio_rate=48_000,
            freq=99.9,
            host="127.0.0.1",
            port=1234,
            gain=None,
        )
        live_args = argparse.Namespace(
            file=None,
            fs=1_200_000,
            audio_rate=48_000,
            freq=99.9,
            host="127.0.0.1",
            port=1234,
            gain=12.0,
        )
        assert cli.cmd_play(file_args) == 0
        assert cli.cmd_play(live_args) == 0

        play_live.side_effect = RuntimeError("audio unavailable")
        assert cli.cmd_play(live_args) == 2
        assert "audio unavailable" in capsys.readouterr().err

    def test_python_m_entrypoint_exits_with_main_return_code(self):
        with patch("rdsclock.cli.main", return_value=7), pytest.raises(SystemExit) as exc:
            runpy.run_module("rdsclock.__main__", run_name="__main__")
        assert exc.value.code == 7


class TestChannelizerCoverage:
    def test_extract_channel_rejects_upsampling(self):
        with pytest.raises(ValueError, match="fs_out must be <="):
            extract_channel(
                np.ones(8, dtype=np.complex64),
                fs_wide=250_000,
                f_center=0.0,
                f_channel=0.0,
                fs_out=500_000,
            )

    def test_decode_channels_reports_progress(self, monkeypatch):
        messages = []
        monkeypatch.setattr(
            "rdsclock.channelizer.extract_channel",
            lambda iq_wide, fs_wide, f_center, f_channel, fs_out: np.ones(8, dtype=np.complex64),
        )
        monkeypatch.setattr("rdsclock.channelizer.decode_iq", lambda iq_chan, fs: _decode_result())
        results = decode_channels(
            iq_wide=np.ones(16, dtype=np.complex64),
            fs_wide=250_000,
            f_center=0.0,
            channels=[SimpleNamespace(freq_hz=1.0, label="A")],
            progress=messages.append,
            max_workers=1,
        )
        assert len(results) == 1
        assert messages == ["channel A: extract", "channel A: decode"]

    def test_auto_center_and_required_bandwidth_reject_empty_inputs(self):
        with pytest.raises(ValueError, match="non-empty"):
            auto_center([])
        with pytest.raises(ValueError, match="non-empty"):
            required_bandwidth([])


class TestPlotCoverage:
    def test_plot_functions_report_missing_matplotlib(self):
        iq = np.ones(2_048, dtype=np.complex64)
        with patch("builtins.__import__", side_effect=_import_without_matplotlib):
            with pytest.raises(RuntimeError, match="matplotlib"):
                plot.plot_mpx_spectrum(iq, fs=250_000)
            with pytest.raises(RuntimeError, match="matplotlib"):
                plot.plot_iq_waterfall(iq, fs=250_000)

    def test_plot_show_and_pilot_failure_paths(self, monkeypatch, tmp_path):
        matplotlib = pytest.importorskip("matplotlib")
        matplotlib.use("Agg")
        iq = np.exp(1j * 2 * np.pi * 1_000 * np.arange(20_000) / 250_000).astype(np.complex64)
        show = Mock()
        monkeypatch.setattr("matplotlib.pyplot.show", show)
        monkeypatch.setattr(
            plot.dsp, "estimate_pilot_19khz", Mock(side_effect=RuntimeError("no pilot"))
        )
        out1 = plot.plot_mpx_spectrum(iq, fs=250_000, out_path=str(tmp_path / "mpx.png"), show=True)
        out2 = plot.plot_iq_waterfall(iq, fs=250_000, out_path=str(tmp_path / "wf.png"), show=True)
        assert out1.endswith("mpx.png")
        assert out2.endswith("wf.png")
        assert show.call_count == 2


class TestRtlTcpCoverage:
    def test_connect_returns_existing_info_and_rejects_bad_magic(self, monkeypatch):
        client = RtlTcpClient()
        info = DongleInfo(magic=b"RTL0", tuner_type=1, gain_count=2)
        client._sock = object()
        client.info = info
        assert client.connect() is info

        class FakeSocket:
            def settimeout(self, timeout):
                self.timeout = timeout

            def close(self):
                self.closed = True

        monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: FakeSocket())
        monkeypatch.setattr(
            RtlTcpClient, "_recv_exact", staticmethod(lambda sock, n: b"BAD!" + b"\x00" * 8)
        )
        with pytest.raises(OSError, match="unexpected magic"):
            RtlTcpClient(host="127.0.0.1", port=1).connect()

    def test_recv_exact_and_send_cmd_require_connection(self):
        class ClosedSocket:
            def recv(self, n):
                return b""

        with pytest.raises(OSError, match="socket closed"):
            RtlTcpClient._recv_exact(ClosedSocket(), 4)

        client = RtlTcpClient()
        with pytest.raises(RuntimeError, match="not connected"):
            client.set_frequency(100)
        with pytest.raises(RuntimeError, match="not connected"):
            client.read_iq(1)


class TestTimeConsensusCoverage:
    def test_empty_track_properties_and_outlier_only_notes(self):
        track = StationTrack(freq_hz=92e6, pi=0x3201)
        assert track.last is None
        assert track.estimated_utc_now(0.0) is None
        assert track.age_s(0.0) == float("inf")
        assert time_consensus._consensus_notes(0.0, 0) == "outlier-only"

    def test_consensus_returns_empty_when_estimates_list_is_empty(self, monkeypatch):
        consensus = TimeConsensus()
        monkeypatch.setattr(
            consensus, "active_tracks", lambda monotonic_now: [StationTrack(freq_hz=1.0, pi=None)]
        )
        monkeypatch.setattr(time_consensus, "_collect_estimates", lambda active, monotonic_now: [])
        result = consensus.consensus(monotonic_now=0.0)
        assert result.utc is None
