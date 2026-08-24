"""Mapping a position in the transcript back to the moment in the audio."""

from __future__ import annotations

import pytest

from radreport.adapters.asr.base import Word
from radreport.pipeline.timing import audio_span, build_timing_map, coverage

TEXT = "the liver is normal no focal lesion no mass seen"


def _words(text: str, step: int = 400) -> list[Word]:
    words = []
    for index, token in enumerate(text.split()):
        words.append(Word(text=token, start_ms=index * step, end_ms=(index + 1) * step))
    return words


def test_each_word_gets_its_own_character_range() -> None:
    timings = build_timing_map(TEXT, _words(TEXT))
    assert len(timings) == len(TEXT.split())
    for timing in timings:
        assert TEXT[timing.char_start : timing.char_end] == TEXT[timing.char_start : timing.char_end].lower()
        assert timing.end_ms > timing.start_ms


def test_a_repeated_word_binds_to_the_right_occurrence() -> None:
    """Searching the whole string would bind both "no"s to the first one's timing, putting the audio cursor in the wrong sentence."""
    timings = build_timing_map(TEXT, _words(TEXT))
    nos = [t for t in timings if TEXT[t.char_start : t.char_end] == "no"]

    assert len(nos) == 2
    assert nos[0].char_start != nos[1].char_start
    assert nos[0].start_ms < nos[1].start_ms


def test_a_mid_transcript_quote_does_not_resolve_to_zero() -> None:
    """The bug this module fixes, stated directly."""
    timings = build_timing_map(TEXT, _words(TEXT))
    quote = "focal lesion"
    start = TEXT.index(quote)

    start_ms, end_ms, interpolated = audio_span(timings, start, start + len(quote))
    assert start_ms > 0
    assert end_ms > start_ms
    assert interpolated is False


def test_a_span_covering_several_words_takes_their_full_range() -> None:
    timings = build_timing_map(TEXT, _words(TEXT))
    start_ms, end_ms, _ = audio_span(timings, 0, len("the liver is normal"))

    assert start_ms == 0
    assert end_ms == 4 * 400


def test_an_unmatched_range_interpolates_between_its_neighbours() -> None:
    """Post-correction and ROVER both produce text no ASR word was located in."""
    timings = build_timing_map(TEXT, _words(TEXT))
    # A range inside the whitespace between two words.
    gap = TEXT.index("is") - 1
    start_ms, end_ms, interpolated = audio_span(timings, gap, gap + 1)

    assert interpolated is True
    assert start_ms <= end_ms


def test_an_empty_timing_map_reports_unknown_rather_than_zero_ms() -> None:
    """`(0, 0, True)` says "unknown". The flag is what tells it apart from a genuine span at the start of the recording."""
    assert audio_span([], 0, 10) == (0, 0, True)


def test_words_absent_from_the_transcript_are_skipped_not_guessed() -> None:
    """Normal after post-correction rewrote a variant."""
    corrected = TEXT.replace("echotexture", "x")
    timings = build_timing_map(corrected, _words(TEXT + " nonexistentword"))
    assert all(corrected[t.char_start : t.char_end] in corrected for t in timings)


def test_coverage_reports_how_much_of_the_transcript_was_located() -> None:
    """A low figure is a fact about the run, not about the reviewer clicking."""
    full = coverage(build_timing_map(TEXT, _words(TEXT)), TEXT)
    assert 0.7 < full <= 1.0
    assert coverage([], TEXT) == 0.0


def test_an_engine_with_no_word_timings_degrades_without_crashing() -> None:
    """Not every engine returns word timings; the pipeline must still run."""
    assert build_timing_map(TEXT, []) == []
    assert audio_span(build_timing_map(TEXT, []), 0, 5) == (0, 0, True)


@pytest.mark.asyncio
async def test_utterances_receive_real_audio_bounds_from_the_asr_stage() -> None:
    """End to end through the two stages that consume the map."""
    import uuid

    from radreport.adapters.asr.whisper_local import StubASREngine
    from radreport.adapters.storage.object_store import InMemoryObjectStore
    from radreport.devtools.synthetic import synth_audio
    from radreport.pipeline.stages.asr import AsrStage
    from radreport.pipeline.stages.providers import StaticKnowledgeProvider, TenantKnowledge
    from radreport.pipeline.stages.segment import SegmentStage
    from radreport.pipeline.state import PipelineState

    tenant = uuid.uuid4()
    store = InMemoryObjectStore()
    store.put("a.flac", synth_audio(seconds=4), content_type="audio/flac")
    state = PipelineState(tenant_id=tenant, recording_id=uuid.uuid4(), pipeline_run_id=uuid.uuid4(), audio_object_key="a.flac")
    knowledge = StaticKnowledgeProvider(TenantKnowledge(tenant_id=tenant))

    after_asr = await AsrStage(StubASREngine(text=TEXT), store, knowledge).run(state, ctx=object())
    assert after_asr.output.word_timings, "the ASR stage must carry timings forward"

    after_segment = await SegmentStage(client=None).run(after_asr.output, ctx=object())
    utterances = after_segment.output.utterances
    assert utterances
    assert any(u.audio_end_ms > 0 for u in utterances)
