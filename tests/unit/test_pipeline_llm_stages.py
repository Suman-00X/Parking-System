"""The three stages that call a language model: splitting the dictation, sketching the findings, and extracting the fields."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import pytest

from radreport.adapters.llm.base import LLMResponse, Usage
from radreport.core.types import AssertionStatus, FillSource, UtteranceLabel
from radreport.pipeline.stages.extract import SectionSpec, build_prompt, merge_samples, parse_fields
from radreport.pipeline.stages.segment import INCLUDED_LABELS, SegmentStage, apply_inclusion, fallback_segments, parse_response
from radreport.pipeline.stages.sketch import SketchStage, fallback_sketch
from radreport.pipeline.state import PipelineState, TranscriptState, Utterance

TRANSCRIPT = "the left kidney contains a simple cyst measuring 3.2 cm. no hydronephrosis."


def _state(text: str = TRANSCRIPT) -> PipelineState:
    return PipelineState(tenant_id=uuid.uuid4(), recording_id=uuid.uuid4(), pipeline_run_id=uuid.uuid4(), transcript=TranscriptState(text=text))


@dataclass
class _Ctx:
    """Minimal `StageContext`: resolves a model, records nothing."""

    async def resolve_model(self, task_key: str):
        @dataclass
        class _Ref:
            model_id: str = "claude-sonnet-5"

        @dataclass
        class _Resolved:
            ref: _Ref

        return _Resolved(ref=_Ref())

    def record_cost(self, usd: float) -> None:  # pragma: no cover
        pass


class _ScriptedClient:
    """Returns canned texts in order, and counts calls."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.calls = 0

    async def complete(self, request, *, model_id: str) -> LLMResponse:
        text = self._texts[min(self.calls, len(self._texts) - 1)]
        self.calls += 1
        return LLMResponse(text=text, usage=Usage(input_tokens=100, output_tokens=50, cache_read_input_tokens=80 if self.calls > 1 else 0, cache_creation_input_tokens=80 if self.calls == 1 else 0), model_id=model_id, cost_usd=0.001)


# ================================================ stage 5: segmentation ======
@pytest.mark.asyncio
async def test_segmentation_labels_without_deleting(monkeypatch) -> None:
    """I2. A misclassified span stays visible and re-includable; a deleted one is gone and nobody can see what is missing."""
    response = json.dumps({"utterances": [{"char_start": 0, "char_end": 53, "label": "report_content", "confidence": 0.95}, {"char_start": 54, "char_end": 74, "label": "aside", "confidence": 0.8}]})
    state = _state()
    result = await SegmentStage(_ScriptedClient([response])).run(state, _Ctx())

    utterances = result.output.utterances
    assert len(utterances) == 2
    # The aside is kept, labelled, and excluded from grounding — not removed.
    assert utterances[1].label == UtteranceLabel.ASIDE
    assert utterances[1].is_included_downstream is False
    assert utterances[1].text == TRANSCRIPT[54:74]


def test_uncertain_spans_cannot_ground_a_field() -> None:
    """A span the classifier could not place must not silently support a clinical claim."""
    assert UtteranceLabel.UNCERTAIN not in INCLUDED_LABELS
    assert UtteranceLabel.OTHER_SPEAKER not in INCLUDED_LABELS
    assert UtteranceLabel.REPORT_CONTENT in INCLUDED_LABELS


def test_an_out_of_range_offset_is_dropped_from_segmentation_not_trusted() -> None:
    """Trusting an unverified offset would let provenance cite a range that means something else entirely."""
    response = json.dumps({"utterances": [{"char_start": 0, "char_end": 9999, "label": "report_content"}]})
    assert parse_response(response, TRANSCRIPT) == []


def test_an_unknown_label_becomes_uncertain_rather_than_report_content() -> None:
    response = json.dumps({"utterances": [{"char_start": 0, "char_end": 10, "label": "invented_label"}]})
    parsed = parse_response(response, TRANSCRIPT)
    assert parsed[0].label == UtteranceLabel.UNCERTAIN


@pytest.mark.asyncio
async def test_the_fallback_includes_everything_rather_than_guessing() -> None:
    """Excluding spans a reviewer cannot see is the unrecoverable direction; including spans that should have been excluded is visible."""
    state = _state()
    result = await SegmentStage(client=None).run(state, _Ctx())

    assert result.output.utterances
    assert all(u.is_included_downstream for u in result.output.utterances)
    assert any("fell back" in w for w in result.warnings)


def test_inclusion_is_derived_from_the_label_in_one_place() -> None:
    utterances = [Utterance(seq=0, char_start=0, char_end=5, audio_start_ms=0, audio_end_ms=1, text="a", label=UtteranceLabel.REPORT_CONTENT, is_included_downstream=False), Utterance(seq=1, char_start=5, char_end=10, audio_start_ms=0, audio_end_ms=1, text="b", label=UtteranceLabel.COMMAND, is_included_downstream=True)]
    apply_inclusion(utterances)
    assert [u.is_included_downstream for u in utterances] == [True, False]


def test_fallback_segments_preserve_offsets() -> None:
    for u in fallback_segments(TRANSCRIPT):
        assert TRANSCRIPT[u.char_start : u.char_end] == u.text


# ==================================================== stage 8: sketch ========
@pytest.mark.asyncio
async def test_the_sketch_runs_on_included_spans_only(monkeypatch) -> None:
    """A retracted span becoming an assertion would read as an orphan and impugn a correct template."""
    state = _state()
    state.utterances = [Utterance(seq=0, char_start=0, char_end=53, audio_start_ms=0, audio_end_ms=1, text="the left kidney contains a simple cyst measuring 3.2 cm", label=UtteranceLabel.REPORT_CONTENT), Utterance(seq=1, char_start=54, char_end=74, audio_start_ms=0, audio_end_ms=1, text="no hydronephrosis", label=UtteranceLabel.OTHER_SPEAKER, is_included_downstream=False)]
    result = await SketchStage(client=None).run(state, _Ctx())

    joined = " ".join(result.output.sketch.assertions)
    assert "cyst" in joined
    assert "hydronephrosis" not in joined


def test_the_fallback_sketch_over_lists_rather_than_under_lists() -> None:
    """A spurious assertion makes a false wrong-template signal a human resolves; a missed one makes silence nobody sees."""
    sketch = fallback_sketch(TRANSCRIPT)
    assert sketch.assertions
    assert any(m["unit"] == "cm" and m["value"] == 3.2 for m in sketch.measurements)


def test_a_dictated_negative_is_an_assertion() -> None:
    sketch = fallback_sketch("no pleural effusion. liver is normal.")
    assert len(sketch.assertions) == 2


# =================================================== stage 10: extraction ====
SPEC = SectionSpec(section="FINDINGS", field_keys=("kidney", "hydronephrosis"), json_schema={"type": "object", "properties": {"kidney": {}, "hydronephrosis": {}}})


def _sample(value: str, *, quote: str = "a simple cyst", status: str = "present") -> dict:
    start = TRANSCRIPT.index(quote)
    return {"kidney": {"value_text": value, "assertion_status": status, "quote": quote, "char_start": start, "char_end": start + len(quote)}}


def test_unanimous_samples_score_full_confidence() -> None:
    merged = merge_samples([_sample("simple cyst")] * 3, TRANSCRIPT)
    assert merged.values["kidney"].confidence == 1.0
    assert merged.values["kidney"].is_flagged is False


def test_disagreement_lowers_confidence_rather_than_discarding_the_value() -> None:
    """Discarding minority answers hides exactly the fields worth a reviewer's attention."""
    merged = merge_samples([_sample("simple cyst"), _sample("simple cyst"), _sample("complex cyst")], TRANSCRIPT)
    value = merged.values["kidney"]

    assert value.value_text == "simple cyst"
    assert value.confidence == pytest.approx(2 / 3, abs=1e-3)
    assert value.is_flagged is True
    assert "sample_disagreement" in value.flag_reasons
    assert merged.disagreements["kidney"] == pytest.approx(2 / 3, abs=1e-3)


def test_a_field_most_samples_omitted_is_not_scored_as_unanimous() -> None:
    """The denominator is k, not the number of samples that answered."""
    merged = merge_samples([_sample("simple cyst"), {}, {}], TRANSCRIPT)
    assert merged.values["kidney"].confidence == pytest.approx(1 / 3, abs=1e-3)


def test_an_uncited_value_is_dropped_not_flagged() -> None:
    """I1. Arriving at verification uncited would read as "unverified" when it is in fact unsupported."""
    uncited = {"kidney": {"value_text": "simple cyst", "assertion_status": "present"}}
    merged = merge_samples([uncited] * 3, TRANSCRIPT)

    assert merged.values == {}
    assert merged.dropped_uncited == ["kidney"]


def test_extraction_never_asserts_its_own_grounding() -> None:
    """Stage 11 decides that. Extraction citing and verifying itself would make the check a formality."""
    merged = merge_samples([_sample("simple cyst")] * 3, TRANSCRIPT)
    assert merged.values["kidney"].is_grounded is False
    assert merged.values["kidney"].fill_source == FillSource.DICTATED


def test_agreement_is_on_the_value_not_the_quote() -> None:
    """Two samples citing different sentences for the same finding agree about the finding."""
    a = _sample("simple cyst", quote="a simple cyst")
    b = _sample("simple cyst", quote="simple cyst measuring 3.2 cm")
    merged = merge_samples([a, b, a], TRANSCRIPT)
    assert merged.values["kidney"].confidence == 1.0


def test_a_dictated_negative_survives_as_absent() -> None:
    """Presence is four-state. `absent` is a statement, not an omission."""
    quote = "no hydronephrosis"
    start = TRANSCRIPT.index(quote)
    sample = {"hydronephrosis": {"value_text": None, "assertion_status": AssertionStatus.ABSENT, "quote": quote, "char_start": start, "char_end": start + len(quote)}}
    merged = merge_samples([sample] * 3, TRANSCRIPT)
    assert merged.values["hydronephrosis"].assertion_status == AssertionStatus.ABSENT


def test_fields_outside_the_section_are_ignored() -> None:
    """A section's call must not write another section's fields."""
    payload = json.dumps({"fields": {"kidney": {}, "liver": {}}})
    parsed = parse_fields(payload, frozenset({"kidney"}))
    assert set(parsed) == {"kidney"}


def test_the_prompt_puts_the_transcript_after_the_cache_breakpoint() -> None:
    """Plan: schema and glossary are identical for every report on this template; the transcript is not."""
    prompt = build_prompt(SPEC, TRANSCRIPT, {"L M P": "LMP"})
    stable = prompt.stable_text()

    assert "LMP" in stable, "the resolved glossary belongs in the cached prefix"
    assert TRANSCRIPT not in stable
    assert TRANSCRIPT in prompt.volatile_text()

    blocks = prompt.to_content_blocks()
    cached = [i for i, b in enumerate(blocks) if "cache_control" in b]
    assert len(cached) == 1, "exactly one breakpoint, at the end of the stable region"
    assert cached[0] == len(prompt.stable) - 1
