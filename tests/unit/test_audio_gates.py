"""The ingest quality checks, split between outright rejections (lossy, unsupported formats) and warnings (sample rate, noise, silence, duration)."""

from __future__ import annotations

import pytest

from radreport.core.config import AudioGateSettings
from radreport.core.errors import IngestRejected
from radreport.devtools.synthetic import synth_audio, synth_lossy_audio
from radreport.ingest.audio_gates import probe_audio, sniff_container


@pytest.fixture
def gates() -> AudioGateSettings:
    return AudioGateSettings()


def test_clean_flac_passes_with_no_warnings(gates: AudioGateSettings) -> None:
    probe = probe_audio(synth_audio(seconds=30, snr_db=25, silence_ratio=0.05), gates)
    assert probe.audio_format == "flac"
    assert probe.sample_rate_hz == 16_000
    assert probe.warnings == []


def test_wav_is_accepted(gates: AudioGateSettings) -> None:
    probe = probe_audio(synth_audio(seconds=20, audio_format="wav"), gates)
    assert probe.audio_format == "wav"


def test_lossy_audio_is_rejected_and_named(gates: AudioGateSettings) -> None:
    """Rejected at ingest because it irreversibly destroys the spectral detail ASR fine-tuning depends on."""
    with pytest.raises(IngestRejected) as exc:
        probe_audio(synth_lossy_audio(), gates)
    assert exc.value.code == "lossy_codec"
    assert "lossy" in exc.value.reason.lower()


@pytest.mark.parametrize("data,expected", [(b"fLaC\x00\x00", "flac"), (b"RIFF....WAVE", "wav"), (b"ID3\x04\x00", "mp3"), (b"\xff\xfb\x90\x00", "mp3"), (b"OggS\x00\x02", "ogg"), (b"\x00\x00\x00\x20ftypM4A ", "m4a"), (b"totally unknown bytes", None)])
def test_container_sniffing(data: bytes, expected: str | None) -> None:
    assert sniff_container(data) == expected


def test_low_sample_rate_warns_but_does_not_reject(gates: AudioGateSettings) -> None:
    """A warning, not a reject: the dictation still has clinical value."""
    probe = probe_audio(synth_audio(seconds=20, sample_rate=8_000), gates)
    assert any("sample rate" in w for w in probe.warnings)


def test_left_running_mic_is_flagged(gates: AudioGateSettings) -> None:
    """The aside contamination — the thing a push-to-talk button solves in hardware rather than in a classifier."""
    probe = probe_audio(synth_audio(seconds=60, snr_db=25, silence_ratio=0.92, seed=2), gates)
    assert probe.silence_ratio is not None and probe.silence_ratio > gates.max_silence_ratio
    assert any("silence" in w for w in probe.warnings)


def test_noisy_recording_is_flagged(gates: AudioGateSettings) -> None:
    """SNR correlates with error rate."""
    probe = probe_audio(synth_audio(seconds=30, snr_db=2, silence_ratio=0.1, seed=3), gates)
    assert any("SNR" in w for w in probe.warnings)


def test_a_fully_silent_recording_does_not_pass_silently(gates: AudioGateSettings) -> None:
    """The case where the silence ratio cannot help: no speech means no level to measure against, so the SNR line has to catch it."""
    probe = probe_audio(synth_audio(seconds=30, snr_db=25, silence_ratio=0.99, seed=4), gates)
    assert probe.warnings, "an empty recording must produce at least one warning"


def test_short_recording_warns(gates: AudioGateSettings) -> None:
    probe = probe_audio(synth_audio(seconds=2), gates)
    assert any("duration" in w for w in probe.warnings)


def test_oversized_upload_is_rejected(gates: AudioGateSettings) -> None:
    small = AudioGateSettings(max_upload_bytes=1024)
    with pytest.raises(IngestRejected) as exc:
        probe_audio(synth_audio(seconds=30), small)
    assert exc.value.code == "too_large"


def test_probe_fields_map_onto_the_recording_row(gates: AudioGateSettings) -> None:
    """Every measurement is stored, so "are error rates worse on the noisy quartile?" is answerable from data rather than by re-running."""
    fields = probe_audio(synth_audio(seconds=20), gates).as_recording_fields()
    assert set(fields) == {"duration_seconds", "sample_rate_hz", "channels", "codec", "audio_format", "measured_snr_db", "silence_ratio"}
