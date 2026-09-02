"""Stage 1: checks the recording is usable -- finds the speech, measures the noise, and normalises the volume.

Order: detect where someone is actually speaking (detect_speech_regions), then report the
result (PreprocessOutput).
"""

from __future__ import annotations

from dataclasses import dataclass

from radreport.adapters.storage.object_store import ObjectStore
from radreport.core.logging import get_logger
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.state import AudioMeta, PipelineState

log = get_logger(__name__)

#: Frames this far below the recording's own speech level are not speech.
SPEECH_FRACTION = 0.25

#: Speech shorter than this is a cough or a door, not a dictation.
MIN_SPEECH_MS = 300
#: Gaps shorter than this are within-sentence pauses; bridging them keeps a sentence in one region rather than splitting it mid-phrase, which would put a provenance boundary inside a clause.
MAX_BRIDGE_GAP_MS = 400
#: Keep a little audio either side of a region. A hard cut on the threshold
#: clips plosives, and a clipped "no" is the worst possible word to lose.
PAD_MS = 120

FRAME_MS = 20


@dataclass(frozen=True, slots=True)
class SpeechRegion:
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True, slots=True)
class PreprocessOutput:
    regions: tuple[SpeechRegion, ...]
    speech_ms: int
    total_ms: int
    trimmed_ms: int
    snr_db: float | None
    silence_ratio: float | None
    peak_normalised: bool

    @property
    def speech_fraction(self) -> float:
        return self.speech_ms / self.total_ms if self.total_ms else 0.0


def detect_speech_regions(signal, sample_rate: int, *, frame_ms: int = FRAME_MS) -> tuple[list[SpeechRegion], float | None, float | None]:
    """Frame-level VAD returning merged regions, SNR and silence ratio."""
    import numpy as np

    samples_per_frame = max(1, int(sample_rate * frame_ms / 1000))
    usable = (len(signal) // samples_per_frame) * samples_per_frame
    if usable == 0:
        return [], None, None

    frames = np.asarray(signal[:usable], dtype="float64").reshape(-1, samples_per_frame)
    power = np.maximum((frames**2).mean(axis=1), 1e-12)
    db = 10.0 * np.log10(power)

    noise_level = float(np.percentile(db, 5))
    speech_level = float(np.percentile(db, 95))
    dynamic_range = speech_level - noise_level

    if dynamic_range < 12.0:
        # Too little separation to call anything silence — either continuous speech or uniformly dead air.
        is_speech = np.ones_like(db, dtype=bool)
    else:
        is_speech = db >= noise_level + dynamic_range * SPEECH_FRACTION

    silence_ratio = round(float((~is_speech).mean()), 4)
    if is_speech.any() and (~is_speech).any():
        snr_db = round(10.0 * float(np.log10(power[is_speech].mean() / max(power[~is_speech].mean(), 1e-12))), 2)
    else:
        snr_db = None

    regions = _merge_frames(is_speech, frame_ms, total_ms=int(len(signal) / sample_rate * 1000))
    return regions, snr_db, silence_ratio


def _merge_frames(is_speech, frame_ms: int, *, total_ms: int) -> list[SpeechRegion]:
    """Frames → padded, gap-bridged regions, in that order."""
    raw: list[list[int]] = []
    for index, speech in enumerate(is_speech):
        start = index * frame_ms
        if not speech:
            continue
        if raw and start - raw[-1][1] <= frame_ms:
            raw[-1][1] = start + frame_ms
        else:
            raw.append([start, start + frame_ms])

    bridged: list[list[int]] = []
    for start, end in raw:
        if bridged and start - bridged[-1][1] <= MAX_BRIDGE_GAP_MS:
            bridged[-1][1] = end
        else:
            bridged.append([start, end])

    regions: list[SpeechRegion] = []
    for start, end in bridged:
        if end - start < MIN_SPEECH_MS:
            continue
        regions.append(SpeechRegion(start_ms=max(0, start - PAD_MS), end_ms=min(total_ms, end + PAD_MS)))
    return regions


class PreprocessStage:
    """Reads audio, writes nothing."""

    name = "preprocess"
    version = "1.0.0"

    def __init__(self, store: ObjectStore, *, normalise_peak: bool = True) -> None:
        self._store = store
        self._normalise_peak = normalise_peak

    def is_idempotent(self) -> bool:
        return True

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if not state.audio_object_key:
            raise ValueError("no audio_object_key on the pipeline state; the orchestrator must locate the recording before preprocess runs")

        import numpy as np
        import soundfile as sf

        raw = self._store.get(state.audio_object_key)
        import io

        signal, sample_rate = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
        channels = signal.shape[1]
        # Mono for analysis.
        mono = signal.mean(axis=1)

        peak = float(np.max(np.abs(mono))) if mono.size else 0.0
        if self._normalise_peak and 0.0 < peak < 1.0:
            mono = mono / peak

        total_ms = int(len(mono) / sample_rate * 1000) if sample_rate else 0
        regions, snr_db, silence_ratio = detect_speech_regions(mono, sample_rate)
        speech_ms = sum(r.duration_ms for r in regions)

        output = PreprocessOutput(regions=tuple(regions), speech_ms=speech_ms, total_ms=total_ms, trimmed_ms=max(0, total_ms - speech_ms), snr_db=snr_db, silence_ratio=silence_ratio, peak_normalised=self._normalise_peak and 0.0 < peak < 1.0)

        warnings: list[str] = []
        if not regions:
            # Not a failure: the ASR stage still runs on the whole clip.
            warnings.append("no speech regions detected; ASR will run on the full clip")
        elif output.speech_fraction < 0.25 and total_ms > 60_000:
            warnings.append(f"only {output.speech_fraction:.0%} of a {total_ms / 1000:.0f}s recording is speech — likely a mic left running ( PTT)")

        existing = state.audio_meta
        state.audio_meta = AudioMeta(
            duration_seconds=total_ms / 1000,
            sample_rate_hz=sample_rate,
            channels=channels,
            codec=existing.codec if existing else "pcm",
            audio_format=existing.audio_format if existing else "wav",
            # Re-measured here rather than copied from ingest: ingest measured
            # the uploaded bytes, this measured what ASR will actually hear.
            measured_snr_db=snr_db,
            silence_ratio=silence_ratio,
            capture_device_class=(existing.capture_device_class if existing else "legacy"),
            is_push_to_talk=existing.is_push_to_talk if existing else False,
        )

        log.info("preprocess_complete", regions=len(regions), speech_ms=speech_ms, trimmed_ms=output.trimmed_ms, snr_db=snr_db, **ctx.as_log_context() if hasattr(ctx, "as_log_context") else {})
        return StageResult(output=state, confidence=1.0 if regions else 0.5, warnings=warnings)
