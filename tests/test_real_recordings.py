"""Live SDR capture tests — exercise the full pipeline on a real radio.

These tests require a running ``rtl_tcp`` daemon and an FM antenna. They
are gated by the ``real_sdr`` marker and skipped automatically when the
hardware is unreachable. They never assert against fixed station names,
dates or PI codes — those vary by location and time and would make the
tests environment-specific. Instead they verify the *invariants of the
pipeline* on real-world signal:

1. The receiver can connect, configure and pull samples.
2. A capture of a plausibly-strong FM frequency yields a non-empty bit
   stream.
3. If any station in a short band sweep transmits valid Group 4A, the
   decoded UTC year must match the current system year (broad sanity).

The synthetic round-trip tests remain the load-bearing correctness
checks; these complement them with "does it survive contact with the
real RF environment" coverage.
"""

import os
from datetime import UTC, datetime

import numpy as np
import pytest

from rdsclock.decoder import decode_iq
from rdsclock.rtl_tcp import RtlTcpClient

# A small selection of common European FM band frequencies. The tests
# accept any of them; they do not depend on a specific station being on
# air. Operators in other regions can override via the environment
# variable RDSCLOCK_TEST_FREQS (comma-separated MHz list).
DEFAULT_TEST_FREQS_MHZ = [89.0, 92.0, 95.5, 98.3, 100.1, 102.4, 106.8]

SAMPLE_RATE = 250_000
SHORT_DURATION_S = 5.0
SCAN_DURATION_S = 2.0
RTL_TCP_HOST = "localhost"
RTL_TCP_PORT = 1234


def _connect_or_skip() -> RtlTcpClient:
    try:
        client = RtlTcpClient(host=RTL_TCP_HOST, port=RTL_TCP_PORT, connect_timeout=3.0)
        client.connect()
    except OSError as exc:
        pytest.skip(f"rtl_tcp not reachable at {RTL_TCP_HOST}:{RTL_TCP_PORT}: {exc}")
    return client


def _test_freqs_mhz() -> list[float]:
    override = os.environ.get("RDSCLOCK_TEST_FREQS")
    if override:
        return [float(x) for x in override.split(",") if x.strip()]
    return list(DEFAULT_TEST_FREQS_MHZ)


@pytest.mark.real_sdr
@pytest.mark.slow
def test_live_pipeline_runs_without_error():
    """Capture a few seconds from rtl_tcp and verify the decoder runs
    end-to-end without raising. Does not assert on RDS content."""
    client = _connect_or_skip()
    try:
        client.set_sample_rate(SAMPLE_RATE)
        client.set_gain_mode_auto()
        # First frequency from the test list — we don't care which one,
        # only that the pipeline survives whatever it receives.
        client.set_frequency(int(_test_freqs_mhz()[0] * 1e6))
        iq = client.record(SHORT_DURATION_S, SAMPLE_RATE)
    finally:
        client.close()

    assert len(iq) == int(SHORT_DURATION_S * SAMPLE_RATE)
    result = decode_iq(iq, fs=SAMPLE_RATE)
    # The pipeline should always emit some bits, even from pure noise.
    assert result.n_bits > 0
    # Frequency-offset estimate must be finite.
    assert np.isfinite(result.freq_offset_hz)


@pytest.mark.real_sdr
@pytest.mark.slow
def test_live_capture_finds_at_least_one_rds_station():
    """Sweep a small list of common FM frequencies; at least one of them
    should yield non-empty RDS groups (PS or RT, with or without CT).

    Skips if no RDS-carrying station is audible — this can happen in
    heavily attenuated environments or with a poor antenna; it is not a
    decoder failure."""
    client = _connect_or_skip()
    found_rds = False
    try:
        client.set_sample_rate(SAMPLE_RATE)
        client.set_gain_mode_auto()
        for freq_mhz in _test_freqs_mhz():
            client.set_frequency(int(freq_mhz * 1e6))
            iq = client.record(SCAN_DURATION_S, SAMPLE_RATE)
            result = decode_iq(iq, fs=SAMPLE_RATE)
            if result.n_groups > 0:
                found_rds = True
                break
    finally:
        client.close()

    if not found_rds:
        pytest.skip("no RDS station audible on the local antenna")
    assert found_rds


@pytest.mark.real_sdr
@pytest.mark.slow
def test_live_clock_time_year_matches_system():
    """If any station broadcasts a valid Group 4A, the decoded UTC year
    must match the system clock's UTC year (give or take the new-year
    boundary). This is a sanity check — it would fail if the parser
    drifted or the layout changed.

    Skips when no station with valid Clock-Time is audible."""
    client = _connect_or_skip()
    sys_now = datetime.now(UTC)
    ct = None
    try:
        client.set_sample_rate(SAMPLE_RATE)
        client.set_gain_mode_auto()
        for freq_mhz in _test_freqs_mhz():
            client.set_frequency(int(freq_mhz * 1e6))
            iq = client.record(SHORT_DURATION_S, SAMPLE_RATE)
            result = decode_iq(iq, fs=SAMPLE_RATE)
            if result.info.latest_clock is not None:
                ct = result.info.latest_clock
                break
    finally:
        client.close()

    if ct is None:
        pytest.skip("no station with valid Clock-Time audible on the local antenna")

    sys_year = sys_now.year
    assert ct.utc.year in (sys_year - 1, sys_year, sys_year + 1), (
        f"decoded CT year {ct.utc.year} disagrees with system year {sys_year}"
    )
