"""FM audio playback helpers — live RTL-SDR or recorded IQ.

This module is intentionally **separate** from the decoder pipeline:
``decoder`` cares about RDS payload bits, while ``audio`` produces
listenable mono audio. The two paths can run side by side on the
same IQ stream (audio for the operator, RDS for time sync).

The ``sounddevice`` dependency is optional and only imported when an
audio sink is needed; the rest of the package works without it.
"""

import numpy as np
from scipy.signal import firwin, lfilter, lfilter_zi

from .rtl_tcp import RtlTcpClient

DEFAULT_AUDIO_RATE = 48_000
DEFAULT_LIVE_FS = 1_200_000  # 1.2 MS/s → decimation x25 to 48 kHz
DEFAULT_AUDIO_CUTOFF = 16_000  # mono FM upper edge (~15 kHz audio + margin)


def fm_demod(iq: np.ndarray) -> np.ndarray:
    """FM demodulation by instantaneous-phase differentiation."""
    z = iq[1:] * np.conj(iq[:-1])
    return np.angle(z).astype(np.float32)


def design_audio_filter(
    fs_in: int,
    fs_out: int = DEFAULT_AUDIO_RATE,
    cutoff_hz: float = DEFAULT_AUDIO_CUTOFF,
    numtaps: int = 129,
) -> tuple[np.ndarray, int]:
    """Design the mono-audio LPF used during decimation from ``fs_in`` to ``fs_out``.

    Returns ``(taps, decimation_factor)``. ``fs_in`` must be an integer
    multiple of ``fs_out``.
    """
    if fs_in % fs_out != 0:
        raise ValueError(f"SDR sample rate ({fs_in}) must be a multiple of audio rate ({fs_out})")
    decim = fs_in // fs_out
    nyq = fs_in / 2.0
    taps = firwin(numtaps=numtaps, cutoff=cutoff_hz / nyq).astype(np.float32)
    return taps, decim


def fm_audio_from_iq(
    iq: np.ndarray,
    fs_in: int,
    fs_out: int = DEFAULT_AUDIO_RATE,
    cutoff_hz: float = DEFAULT_AUDIO_CUTOFF,
    normalise: bool = True,
) -> np.ndarray:
    """Convert an IQ buffer into a mono audio float32 buffer at ``fs_out``.

    Steps: FM phase demodulation → LPF (cutoff = ``cutoff_hz``) → decimation
    to ``fs_out`` → optional peak normalisation to ±0.5 to avoid clipping.
    """
    taps, decim = design_audio_filter(fs_in, fs_out, cutoff_hz=cutoff_hz)
    fm = fm_demod(iq)
    audio, _ = lfilter(taps, 1.0, fm), None
    audio = audio[::decim]
    if normalise:
        peak = float(np.max(np.abs(audio)) + 1e-6)
        audio = (audio / peak) * 0.5
    return audio.astype(np.float32)


def play_iq_live(
    freq_mhz: float,
    host: str = "localhost",
    port: int = 1234,
    fs_sdr: int = DEFAULT_LIVE_FS,
    fs_audio: int = DEFAULT_AUDIO_RATE,
    chunk_samples: int | None = None,
    gain_db: float | None = None,
) -> None:
    """Stream live FM audio from an RTL-SDR via rtl_tcp.

    Press Ctrl+C to stop. Requires ``sounddevice``.
    """
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError(
            "live FM playback requires the optional 'sounddevice' dependency; "
            "install with: pip install 'rdsclock[audio]'"
        ) from exc

    chunk_samples = chunk_samples or fs_sdr // 10  # 100 ms per block
    taps, decim = design_audio_filter(fs_sdr, fs_audio)
    zi = lfilter_zi(taps, 1.0)

    sd.default.samplerate = fs_audio
    sd.default.channels = 1

    print(f"Connecting to rtl_tcp {host}:{port}…")
    with RtlTcpClient(host=host, port=port) as client:
        info = client.info
        print(f"  Tuner: {info.tuner_type}, gain count: {info.gain_count}")
        client.set_sample_rate(fs_sdr)
        if gain_db is None:
            client.set_gain_mode_auto()
            print("  Gain: AGC")
        else:
            client.set_gain_mode_manual(int(gain_db * 10))
            print(f"  Gain: manual {gain_db} dB")
        client.set_frequency(int(freq_mhz * 1e6))
        # Discard a warm-up chunk so AGC/PLL settle before audio starts.
        _ = client.read_iq(chunk_samples, settle_s=0.2)
        print(
            f"Playing FM {freq_mhz} MHz @ {fs_sdr / 1e6:.2f} MS/s "
            f"→ audio {fs_audio} Hz (Ctrl+C to stop)…"
        )

        with sd.OutputStream(dtype="float32") as stream:
            try:
                while True:
                    iq = client.read_iq(chunk_samples, settle_s=0.0)
                    fm = fm_demod(iq)
                    audio, zi = lfilter(taps, 1.0, fm, zi=zi)
                    audio = audio[::decim]
                    peak = float(np.max(np.abs(audio)) + 1e-6)
                    audio = (audio / peak) * 0.5
                    stream.write(audio.astype("float32"))
            except KeyboardInterrupt:
                print("\nInterrupted by operator (Ctrl+C).")


def play_iq_file(
    path: str,
    fs_in: int = 250_000,
    fs_audio: int = DEFAULT_AUDIO_RATE,
) -> None:
    """Play back a previously captured ``.iq`` file as mono FM audio.

    Autodetects the on-disk format (``uint8`` rtl_sdr vs ``complex64``)
    using the same heuristic as :func:`rdsclock.decoder.decode_file`.
    """
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError(
            "audio playback requires the optional 'sounddevice' dependency; "
            "install with: pip install 'rdsclock[audio]'"
        ) from exc

    from . import dsp

    iq = dsp.read_iq_complex64(path)
    if len(iq) == 0 or np.max(np.abs(iq[:1000])) > 100:
        iq = dsp.read_iq_u8(path)

    # If the file was captured at the standard 250 kS/s, decimate by 5 to
    # reach 50 kS/s, then by ~1.04 to 48 kHz. We keep it simple: design a
    # filter using a chosen integer ratio and accept whatever audio rate
    # the file allows.
    if fs_in % fs_audio == 0:
        audio = fm_audio_from_iq(iq, fs_in=fs_in, fs_out=fs_audio)
    else:
        # Fall back: drop to the largest fs_audio divisor of fs_in.
        from math import gcd

        target = gcd(fs_in, fs_audio)
        audio = fm_audio_from_iq(iq, fs_in=fs_in, fs_out=target)
        fs_audio = target

    duration_s = len(audio) / fs_audio
    print(f"Playing {path}: {duration_s:.1f}s of audio @ {fs_audio} Hz (Ctrl+C to stop)…")
    try:
        sd.play(audio, samplerate=fs_audio, blocking=True)
    except KeyboardInterrupt:
        sd.stop()
        print("\nInterrupted by operator (Ctrl+C).")
