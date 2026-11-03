"""Test of the digital channelizer + multi-station decode (synthetic)."""

import threading
from datetime import UTC, datetime

import numpy as np
import pytest

from rdsclock import channelizer
from rdsclock.channelizer import (
    ChannelSpec,
    auto_center,
    choose_center,
    decode_channels,
    extract_channel,
    outside_capture,
    required_bandwidth,
)
from rdsclock.rds_groups import encode_group_4a, encode_ps_groups
from rdsclock.synth import synthesize_fm_iq


def _mix_stations(stations, fs_wide, f_center, duration_s, rng=None):
    """Assemble a synthetic wideband signal from stations."""
    n_samples = int(round(duration_s * fs_wide))
    n_index = np.arange(n_samples)
    wide = np.zeros(n_samples, dtype=np.complex64)
    for spec, iq_narrow_250k in stations:
        # Upsample to fs_wide
        from math import gcd

        from scipy.signal import resample_poly

        a, b = int(fs_wide), 250_000
        g = gcd(a, b)
        up, down = a // g, b // g
        iq_up = resample_poly(iq_narrow_250k, up, down).astype(np.complex64)
        n_use = min(len(iq_up), n_samples)
        # Mix to the target frequency: e^{j 2π Δf · t}
        delta = spec.freq_hz - f_center
        t = n_index[:n_use] / fs_wide
        wide[:n_use] += iq_up[:n_use] * np.exp(1j * 2 * np.pi * delta * t).astype(np.complex64)
    if rng is not None:
        # Add shared white noise (lightly)
        noise = (rng.standard_normal(n_samples) + 1j * rng.standard_normal(n_samples)) * 0.02
        wide += noise.astype(np.complex64)
    return wide


def _build_station(pi: int, ps: str, ct_utc: datetime) -> bytes:
    groups = encode_ps_groups(pi=pi, ps_name=ps) + [encode_group_4a(pi=pi, clock_time_local=ct_utc)]
    return groups


class TestExtractChannel:
    def test_picks_only_target(self):
        """In baseband (after the SDR mixer with f_center), tones appear at
        (f_channel - f_center). A 95.5 MHz tone with SDR @ 96 MHz = -0.5 MHz
        in wide."""
        fs_wide = 2_000_000
        n = np.arange(fs_wide).astype(np.float64)
        f_center = 96_000_000  # SDR center (absolute)
        f_target = 95_500_000  # absolute tone frequency
        f_other = 96_500_000  # second tone

        delta_target = f_target - f_center  # -500 kHz in baseband
        delta_other = f_other - f_center  # +500 kHz in baseband

        wave_target = np.exp(1j * 2 * np.pi * delta_target * n / fs_wide)
        wave_other = np.exp(1j * 2 * np.pi * delta_other * n / fs_wide)
        wide = (wave_target + wave_other).astype(np.complex64)

        chan = extract_channel(
            wide, fs_wide, f_center, f_channel=f_target, fs_out=250_000, half_bw=80_000
        )
        # After channelization, 95.5 should be at DC (amplitude ~1).
        # The other (96.5) is shifted by +1 MHz in the new reference -> filtered out.
        mean_abs = float(np.mean(np.abs(chan[200:])))
        assert 0.7 < mean_abs < 1.3

    def test_mixing_blocks_join_without_phase_jumps(self, monkeypatch):
        rng = np.random.default_rng(3)
        wide = (rng.standard_normal(60_000) + 1j * rng.standard_normal(60_000)).astype(np.complex64)
        whole = extract_channel(wide, 2_400_000, 102.85e6, 102.4e6)
        monkeypatch.setattr(channelizer, "_MIX_BLOCK", 7_001)
        blocks = extract_channel(wide, 2_400_000, 102.85e6, 102.4e6)
        assert blocks.dtype == np.complex64
        np.testing.assert_allclose(blocks, whole, rtol=0, atol=1e-5)

    def test_output_sample_k_belongs_to_input_time_k_over_fs_out(self):
        # Clock-Time receive times count output samples from the capture start.
        fs_wide, f_center, f_channel = 2_400_000, 102.85e6, 102.4e6
        n = np.arange(240_000)
        onset = 120_000
        tone = np.exp(2j * np.pi * (f_channel - f_center) * n / fs_wide)
        wide = np.where(n >= onset, tone, 0).astype(np.complex64)
        chan = extract_channel(wide, fs_wide, f_center, f_channel)
        assert int(np.argmax(np.abs(chan) >= 0.5)) == pytest.approx(
            onset * 250_000 / fs_wide, abs=1
        )

    @pytest.mark.parametrize(("fs_wide", "fs_out"), [(250_000, 250_000), (400_000, 100_000)])
    def test_out_of_band_tones_are_removed_at_any_rate_ratio(self, fs_wide, fs_out):
        # At equal rates resample_poly would skip its filter; below 2 * half_bw
        # the output Nyquist frequency must bound the passband instead.
        n = np.arange(fs_wide)
        tone_hz = 0.3 * fs_out if fs_wide == fs_out else 75_000
        tone = np.exp(2j * np.pi * tone_hz * n / fs_wide).astype(np.complex64)
        chan = extract_channel(tone, fs_wide, 100e6, 100e6, fs_out=fs_out, half_bw=0.15 * fs_out)
        assert chan.dtype == np.complex64
        assert float(np.mean(np.abs(chan[100:-100]))) < 0.05

    def test_a_single_worker_decodes_in_the_calling_thread(self, monkeypatch):
        threads = []

        def decode(iq, fs, capture_start_monotonic_ns=None):
            threads.append(threading.current_thread())
            return "decoded"

        monkeypatch.setattr(channelizer, "decode_iq", decode)
        wide = np.ones(48_000, dtype=np.complex64)
        decode_channels(wide, 2_400_000, 100e6, [ChannelSpec(100e6)] * 2, max_workers=1)
        assert threads == [threading.main_thread()] * 2

    def test_decode_channels_measures_power_and_passes_the_capture_start(self, monkeypatch):
        starts = []

        def decode(iq, fs, capture_start_monotonic_ns=None):
            starts.append(capture_start_monotonic_ns)
            return "decoded"

        monkeypatch.setattr(channelizer, "decode_iq", decode)
        wide = np.full(48_000, 0.1, dtype=np.complex64)
        (result,) = decode_channels(
            wide, 2_400_000, 100e6, [ChannelSpec(100e6)], capture_start_monotonic_ns=123
        )
        assert starts == [123]
        assert result.result == "decoded"
        assert result.rssi_db == pytest.approx(-20.0, abs=0.5)


class TestHelpers:
    def test_auto_center(self):
        assert auto_center([95.5e6, 96.5e6, 97.7e6]) == pytest.approx((95.5 + 97.7) / 2 * 1e6)

    def test_required_bandwidth(self):
        bw = required_bandwidth([95.5e6, 97.7e6], guard_hz=200_000)
        assert bw == pytest.approx(2.2e6 + 2 * 200_000)

    def test_choose_center_keeps_the_dc_offset_clear_of_every_station(self):
        freqs = [102.0e6, 102.4e6, 103.0e6]
        center = choose_center(freqs, 2_400_000)
        assert center == pytest.approx(102.55e6)
        assert min(abs(f - center) for f in freqs) == pytest.approx(150_000)
        assert outside_capture(freqs, 2_400_000, center) == []

    def test_choose_center_moves_off_a_lone_station(self):
        assert choose_center([100.0e6], 2_400_000) == pytest.approx(99.85e6)

    def test_choose_center_falls_back_to_the_middle(self):
        assert choose_center([100.0e6, 100.2e6], 1_000_000) == pytest.approx(100.1e6)

    def test_choose_center_refuses_stations_that_do_not_fit(self):
        with pytest.raises(ValueError, match="do not fit"):
            choose_center([90.0e6, 103.0e6], 2_400_000)

    def test_outside_capture_names_stations_near_the_edges(self):
        assert outside_capture([102.0e6, 103.0e6], 2_400_000, 104.0e6) == [102.0e6]


class TestMultiStationSynthetic:
    @pytest.mark.slow
    def test_three_stations_different_clocks(self):
        """Synthesize 3 stations spaced by 1.5 MHz, take a 5 MHz wide capture,
        decode all of them."""
        f_center = 96.0e6
        fs_wide = 5_000_000  # 5 MS/s - plenty of bandwidth for spaced stations

        ct1 = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
        ct2 = datetime(2026, 5, 16, 12, 15, tzinfo=UTC)
        ct3 = datetime(2026, 5, 16, 12, 30, tzinfo=UTC)

        stations_iq = []
        rng = np.random.default_rng(0)
        for pi, ps, ct, freq_hz in [
            (0x3201, "STN-A   ", ct1, 94.5e6),
            (0x3202, "STN-B   ", ct2, 96.0e6),
            (0x3203, "STN-C   ", ct3, 97.5e6),
        ]:
            groups = _build_station(pi, ps, ct)
            iq = synthesize_fm_iq(groups, duration_s=2.5, fs=250_000, snr_db=None, rng=rng)
            stations_iq.append((ChannelSpec(freq_hz=freq_hz, label=f"{freq_hz / 1e6:.1f}"), iq))

        wide = _mix_stations(stations_iq, fs_wide=fs_wide, f_center=f_center, duration_s=2.5)

        results = decode_channels(
            iq_wide=wide,
            fs_wide=fs_wide,
            f_center=f_center,
            channels=[s[0] for s in stations_iq],
            max_workers=3,
        )

        assert len(results) == 3
        decoded_cts = {}
        for r in results:
            ct = r.result.info.latest_clock
            assert ct is not None, f"missing CT for {r.spec.label}"
            decoded_cts[r.spec.freq_hz] = ct.utc

        assert decoded_cts[94.5e6] == ct1
        assert decoded_cts[96.0e6] == ct2
        assert decoded_cts[97.5e6] == ct3
