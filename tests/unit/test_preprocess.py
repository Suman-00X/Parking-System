"""Finding the speech, measuring the noise and trimming the silence, on real generated audio."""

from __future__ import annotations

import uuid

import pytest

from radreport.adapters.storage.object_store import InMemoryObjectStore
from radreport.devtools.synthetic import synth_audio
from radreport.pipeline.stages.preprocess import MIN_SPEECH_MS, PreprocessStage, detect_speech_regions
from radreport.pipeline.state import PipelineState


def _read(data: bytes):
    import io

    import soundfile as sf

    signal, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
    return signal.mean(axis=1), rate


def test_speech_regions_are_found_in_audio_with_silence() -> None:
    """The `silence_ratio` is dialable in the generator, so the detector is tested at a boundary rather than on a fixture that always passes."""
    signal, rate = _read(synth_audio(seconds=20, snr_db=25, silence_ratio=0.4, seed=1))
    regions, snr_db, silence_ratio = detect_speech_regions(signal, rate)

    assert regions, "audio with 60% speech must yield at least one region"
    assert all(r.duration_ms >= MIN_SPEECH_MS for r in regions)
    assert all(r.start_ms < r.end_ms for r in regions)
    assert silence_ratio is not None and 0.0 < silence_ratio < 1.0
    assert snr_db is not None


def test_regions_are_ordered_and_do_not_overlap() -> None:
    """Overlapping regions would double-bill ASR and duplicate provenance."""
    signal, rate = _read(synth_audio(seconds=30, snr_db=20, silence_ratio=0.3, seed=2))
    regions, _snr, _silence = detect_speech_regions(signal, rate)

    for earlier, later in zip(regions, regions[1:], strict=False):
        assert earlier.end_ms <= later.start_ms


def test_detection_is_reproducible() -> None:
    """Plan: deterministic stages must be bit-reproducible."""
    signal, rate = _read(synth_audio(seconds=15, seed=3))
    first = detect_speech_regions(signal, rate)
    second = detect_speech_regions(signal, rate)

    assert [(r.start_ms, r.end_ms) for r in first[0]] == [(r.start_ms, r.end_ms) for r in second[0]]
    assert first[1] == second[1]


@pytest.mark.asyncio
async def test_the_stage_measures_what_asr_will_hear() -> None:
    """SNR is re-measured here rather than copied from ingest: ingest measured the uploaded bytes, this measures the signal ASR receives."""
    store = InMemoryObjectStore()
    key = "rec/test.flac"
    store.put(key, synth_audio(seconds=20, snr_db=25, silence_ratio=0.4, seed=4), content_type="audio/flac")

    state = PipelineState(tenant_id=uuid.uuid4(), recording_id=uuid.uuid4(), pipeline_run_id=uuid.uuid4(), audio_object_key=key)
    result = await PreprocessStage(store).run(state, ctx=object())

    meta = result.output.audio_meta
    assert meta is not None
    assert meta.sample_rate_hz == 16_000
    assert meta.duration_seconds == pytest.approx(20, abs=0.5)
    assert meta.measured_snr_db is not None
    assert meta.silence_ratio is not None


@pytest.mark.asyncio
async def test_a_mic_left_running_is_reported_not_rejected() -> None:
    """The fix is a push-to-talk button, not a pipeline failure. A long recording that is mostly dead air still gets transcribed."""
    store = InMemoryObjectStore()
    key = "rec/mostly-silence.flac"
    store.put(key, synth_audio(seconds=120, snr_db=20, silence_ratio=0.9, seed=5), content_type="audio/flac")

    state = PipelineState(tenant_id=uuid.uuid4(), recording_id=uuid.uuid4(), pipeline_run_id=uuid.uuid4(), audio_object_key=key)
    result = await PreprocessStage(store).run(state, ctx=object())

    assert result.output.audio_meta.silence_ratio > 0.5
    assert any("mic left running" in w or "no speech" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_a_missing_audio_key_fails_loudly() -> None:
    """Silently transcribing nothing would produce an empty, confident draft."""
    state = PipelineState(tenant_id=uuid.uuid4(), recording_id=uuid.uuid4(), pipeline_run_id=uuid.uuid4())
    with pytest.raises(ValueError, match="audio_object_key"):
        await PreprocessStage(InMemoryObjectStore()).run(state, ctx=object())
