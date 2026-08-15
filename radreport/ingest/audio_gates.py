"""Checks an uploaded recording is usable, keeping "this file is wrong" apart from "this recording is too poor to transcribe".

Order: identify the container and codec (sniff_container, probe_audio) -> measure the audio
(analyse_frames) -> judge the noise and silence (estimate_snr_db, estimate_silence_ratio).
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from typing import Any

from radreport.core.config import AudioGateSettings
from radreport.core.errors import IngestRejected
from radreport.core.types import AudioFormat

#: Containers we accept. Anything else is a reject, not a warning.
_FORMAT_ALIASES = {"flac": AudioFormat.FLAC, "wav": AudioFormat.WAV, "wave": AudioFormat.WAV}

#: Rejected explicitly rather than by omission, so the error names the reason.
LOSSY_FORMATS = frozenset({"mp3", "m4a", "aac", "ogg", "opus", "wma", "amr", "3gp"})

#: Magic-byte signatures, checked *before* handing bytes to the decoder. libsndfile's MPEG support varies by build, so without this a rejected MP3 reports "could not decode audio" — which tells a lab admin nothing about why their upload was refused, and hides the real rule from them.
_MAGIC: tuple[tuple[bytes, str], ...] = ((b"fLaC", "flac"), (b"RIFF", "wav"), (b"ID3", "mp3"), (b"\xff\xfb", "mp3"), (b"\xff\xf3", "mp3"), (b"\xff\xf2", "mp3"), (b"OggS", "ogg"), (b"\x1aE\xdf\xa3", "webm"))


def sniff_container(data: bytes) -> str | None:
    """Best-effort container identification from the first bytes."""
    for signature, name in _MAGIC:
        if data.startswith(signature):
            return name
    # ISO base media (m4a/aac/3gp) puts a size prefix before the brand.
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12].lower()
        if brand.startswith((b"m4a", b"mp4", b"isom", b"3gp")):
            return "m4a"
    return None


@dataclass(slots=True)
class AudioProbe:
    """What the gates measured."""

    duration_seconds: float
    sample_rate_hz: int
    channels: int
    codec: str
    audio_format: str
    measured_snr_db: float | None = None
    silence_ratio: float | None = None
    warnings: list[str] = field(default_factory=list)

    def as_recording_fields(self) -> dict[str, Any]:
        return {"duration_seconds": round(self.duration_seconds, 2), "sample_rate_hz": self.sample_rate_hz, "channels": self.channels, "codec": self.codec, "audio_format": self.audio_format, "measured_snr_db": (round(self.measured_snr_db, 2) if self.measured_snr_db is not None else None), "silence_ratio": (round(self.silence_ratio, 4) if self.silence_ratio is not None else None)}


def probe_audio(data: bytes, settings: AudioGateSettings) -> AudioProbe:
    """Decode, measure, and apply the gates."""
    if len(data) > settings.max_upload_bytes:
        raise IngestRejected(f"upload is {len(data)} bytes, over the {settings.max_upload_bytes} limit", code="too_large")

    # Name the rule before the decoder gets a chance to fail for another reason.
    sniffed = sniff_container(data)
    if sniffed in LOSSY_FORMATS or sniffed == "webm":
        raise IngestRejected(f"{sniffed} is lossy; rejects lossy audio at ingest because it irreversibly destroys the spectral detail ASR fine-tuning depends on. Re-export as {' or '.join(settings.allowed_formats)}", code="lossy_codec")

    try:
        import soundfile as sf
    except ImportError as exc:  # pragma: no cover
        raise IngestRejected("soundfile is required to validate audio", code="no_decoder") from exc

    try:
        with sf.SoundFile(io.BytesIO(data)) as handle:
            container = (handle.format or "").lower()
            subtype = (handle.subtype or "").lower()
            sample_rate = int(handle.samplerate)
            channels = int(handle.channels)
            frames = int(handle.frames)
            samples = handle.read(dtype="float32", always_2d=True)
    except IngestRejected:
        raise
    except Exception as exc:
        raise IngestRejected(f"could not decode audio: {exc}", code="undecodable") from exc

    if container in LOSSY_FORMATS or subtype.startswith("mpeg"):
        raise IngestRejected(f"{container or subtype} is lossy; rejects lossy audio at ingest because it irreversibly destroys the spectral detail ASR fine-tuning depends on", code="lossy_codec")

    audio_format = _FORMAT_ALIASES.get(container)
    if audio_format is None:
        raise IngestRejected(f"unsupported container {container!r}; only {', '.join(settings.allowed_formats)} are accepted", code="unsupported_format")
    if audio_format not in settings.allowed_formats:
        raise IngestRejected(f"{audio_format} is not in the allowed set {settings.allowed_formats}", code="unsupported_format")

    duration = frames / sample_rate if sample_rate else 0.0
    probe = AudioProbe(duration_seconds=duration, sample_rate_hz=sample_rate, channels=channels, codec=subtype or container, audio_format=str(audio_format))

    # ---- warn-level gates -------------------------------------------------
    if sample_rate < settings.min_sample_rate_hz:
        probe.warnings.append(f"sample rate {sample_rate} Hz is below {settings.min_sample_rate_hz} Hz")
    if duration < settings.min_duration_seconds:
        probe.warnings.append(f"duration {duration:.1f}s is below {settings.min_duration_seconds}s")
    if duration > settings.max_duration_seconds:
        probe.warnings.append(f"duration {duration:.1f}s exceeds {settings.max_duration_seconds}s")

    mono = samples.mean(axis=1) if samples.ndim > 1 else samples
    analysis = analyse_frames(mono, sample_rate)
    probe.measured_snr_db = analysis.snr_db
    probe.silence_ratio = analysis.silence_ratio

    if probe.measured_snr_db is not None and probe.measured_snr_db < settings.min_snr_db:
        probe.warnings.append(f"estimated SNR {probe.measured_snr_db:.1f} dB is below {settings.min_snr_db} dB")
    if probe.silence_ratio is not None and probe.silence_ratio > settings.max_silence_ratio:
        probe.warnings.append(f"silence ratio {probe.silence_ratio:.2f} exceeds {settings.max_silence_ratio}")

    return probe


def _frame(signal: Any, sample_rate: int, ms: int = 20) -> Any:
    import numpy as np

    size = max(1, int(sample_rate * ms / 1000))
    usable = (len(signal) // size) * size
    if usable == 0:
        return np.empty((0, size), dtype="float32")
    return signal[:usable].reshape(-1, size)


@dataclass(frozen=True, slots=True)
class FrameAnalysis:
    """SNR and silence, derived from **one** framing pass."""

    snr_db: float | None
    silence_ratio: float | None
    speech_frame_count: int


def analyse_frames(signal: Any, sample_rate: int, *, silence_fraction: float = 0.25, min_dynamic_range_db: float = 12.0) -> FrameAnalysis:
    """Coarse speech/silence split plus an SNR estimate."""
    import numpy as np

    frames = _frame(signal, sample_rate)
    if frames.size == 0:
        return FrameAnalysis(snr_db=None, silence_ratio=None, speech_frame_count=0)

    power = np.maximum((frames.astype("float64") ** 2).mean(axis=1), 1e-12)
    db = 10.0 * np.log10(power)

    # The threshold sits a fixed fraction up from the noise floor toward the speech level, rather than at a fixed dBFS value.
    noise_level = float(np.percentile(db, 5))
    speech_level = float(np.percentile(db, 95))
    dynamic_range = speech_level - noise_level

    if dynamic_range < min_dynamic_range_db:
        # Too little separation to call anything silence.
        is_speech = np.ones_like(db, dtype=bool)
    else:
        is_speech = db >= noise_level + dynamic_range * silence_fraction

    silence_ratio = round(float((~is_speech).mean()), 4)

    if is_speech.any() and (~is_speech).any():
        speech_power = float(power[is_speech].mean())
        noise_power = float(power[~is_speech].mean())
    else:
        # No usable split — fall back to a percentile estimate so the number is
        # still comparable across recordings.
        speech_power = float(np.percentile(power, 90))
        noise_power = float(np.percentile(power, 10))

    if noise_power <= 0 or speech_power <= noise_power:
        snr_db = 0.0
    else:
        snr_db = round(10.0 * math.log10(speech_power / noise_power), 2)

    return FrameAnalysis(snr_db=snr_db, silence_ratio=silence_ratio, speech_frame_count=int(is_speech.sum()))


def estimate_snr_db(signal: Any, sample_rate: int) -> float | None:
    """Estimated speech-to-noise ratio in dB."""
    return analyse_frames(signal, sample_rate).snr_db


def estimate_silence_ratio(signal: Any, sample_rate: int) -> float | None:
    """Fraction of frames well below the recording's own speech level."""
    return analyse_frames(signal, sample_rate).silence_ratio
