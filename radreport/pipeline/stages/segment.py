"""Stage 5: splits the dictation into utterances and labels what each one is, without deleting anything.

Order: build the prompt (build_prompt) -> read the model's answer (parse_response), falling back
to a simple split if it fails (fallback_segments) -> align the pieces to the audio
(apply_audio_bounds) and mark which count as report content (apply_inclusion).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from radreport.adapters.llm.base import LLMClient, LLMRequest
from radreport.adapters.llm.prompt import PromptBundle, VolatileBlock, section_block, system_block
from radreport.core.logging import get_logger
from radreport.core.types import LabelSource, TaskKey, UtteranceLabel
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.state import PipelineState, Utterance
from radreport.pipeline.timing import WordTiming, audio_span

log = get_logger(__name__)

#: Labels whose spans may ground a field. The single definition of "included".
INCLUDED_LABELS: frozenset[str] = frozenset({UtteranceLabel.REPORT_CONTENT, UtteranceLabel.DISFLUENCY})

SYSTEM_PROMPT = """\
You segment a dictated radiology report into utterances and label each one.

Rules:
- Label every span. Never omit, merge away, or rewrite text.
- Preserve the exact character offsets of the source transcript.
- Use `uncertain` when you cannot confidently place a span. It is a real
  answer, not a failure.
- `self_correction` marks the span being RETRACTED, not the correction.
- `other_speaker` is anyone who is not the dictating radiologist.
- `command` is dictation control ("new paragraph", "period", "end of report").
- `aside` is speech not intended for the report.

Return JSON: {"utterances": [{"char_start": int, "char_end": int,
"label": str, "confidence": float}]}"""

SEGMENT_INSTRUCTION = """\
Segment at sentence and clause boundaries. A span must not cross a speaker
change or a self-correction boundary."""

_SENTENCE_SPLIT = re.compile(r"(?<=[.;?!])\s+|\n+")


@dataclass(frozen=True, slots=True)
class SegmentationResult:
    utterances: list[Utterance]
    used_model: bool
    low_confidence_count: int


def build_prompt(transcript: str) -> PromptBundle:
    """Stable system + instruction, volatile transcript."""
    return PromptBundle(stable=[system_block(SYSTEM_PROMPT), section_block(SEGMENT_INSTRUCTION, label="segmentation_rules")], volatile=[VolatileBlock(text=f"<transcript>\n{transcript}\n</transcript>", label="transcript")])


def _timings(state: PipelineState) -> list[WordTiming]:
    return [WordTiming(char_start=t.char_start, char_end=t.char_end, start_ms=t.start_ms, end_ms=t.end_ms) for t in state.word_timings]


def apply_audio_bounds(utterances: list[Utterance], timings: list[WordTiming]) -> None:
    """Give each utterance the audio range its characters occupy."""
    for utterance in utterances:
        start_ms, end_ms, _interpolated = audio_span(timings, utterance.char_start, utterance.char_end)
        utterance.audio_start_ms = start_ms
        utterance.audio_end_ms = end_ms


def fallback_segments(transcript: str) -> list[Utterance]:
    """Sentence-split with everything labelled `report_content`."""
    utterances: list[Utterance] = []
    cursor = 0
    for seq, piece in enumerate(_SENTENCE_SPLIT.split(transcript)):
        if not piece.strip():
            continue
        start = transcript.find(piece, cursor)
        if start < 0:
            start = cursor
        end = start + len(piece)
        cursor = end
        utterances.append(Utterance(seq=seq, char_start=start, char_end=end, audio_start_ms=0, audio_end_ms=0, text=piece, label=UtteranceLabel.REPORT_CONTENT, label_confidence=0.5, label_source=LabelSource.RULE, is_included_downstream=True))
    return utterances


def apply_inclusion(utterances: list[Utterance]) -> None:
    """Derive `is_included_downstream` from the label, in one place."""
    for utterance in utterances:
        utterance.is_included_downstream = utterance.label in INCLUDED_LABELS


def parse_response(text: str, transcript: str) -> list[Utterance]:
    """Turn the model's JSON into utterances, verifying every offset."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        log.warning("segmentation_response_not_json", length=len(text))
        return []

    utterances: list[Utterance] = []
    for seq, item in enumerate(payload.get("utterances", [])):
        start = int(item.get("char_start", -1))
        end = int(item.get("char_end", -1))
        if not (0 <= start < end <= len(transcript)):
            log.warning("segmentation_offset_out_of_range", char_start=start, char_end=end)
            continue
        label = item.get("label", UtteranceLabel.UNCERTAIN)
        if label not in UtteranceLabel.values():
            label = UtteranceLabel.UNCERTAIN
        utterances.append(Utterance(seq=seq, char_start=start, char_end=end, audio_start_ms=0, audio_end_ms=0, text=transcript[start:end], label=label, label_confidence=float(item.get("confidence", 0.5)), label_source=LabelSource.LLM))
    return utterances


class SegmentStage:
    """One model call; falls back to sentence splitting."""

    name = "segment_classify"
    version = "1.0.0"
    task_key = TaskKey.UTTERANCE_CLASSIFICATION

    def __init__(self, client: LLMClient | None = None, *, max_tokens: int = 4096) -> None:
        self._client = client
        self._max_tokens = max_tokens

    def is_idempotent(self) -> bool:
        return self._client is None

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if state.transcript is None:
            raise ValueError("segmentation runs on a transcript; none is present")

        transcript = state.transcript.text
        cost = 0.0
        model_id: str | None = None
        warnings: list[str] = []
        used_model = False

        utterances: list[Utterance] = []
        if self._client is not None:
            resolved = await ctx.resolve_model(self.task_key)
            model_id = resolved.ref.model_id
            response = await self._client.complete(LLMRequest(prompt=build_prompt(transcript), max_tokens=self._max_tokens), model_id=model_id)
            cost = response.cost_usd
            utterances = parse_response(response.text, transcript)
            used_model = bool(utterances)

        if not utterances:
            utterances = fallback_segments(transcript)
            warnings.append("segmentation fell back to sentence splitting; every span is labelled report_content, so nothing is excluded from grounding that a reviewer cannot see")

        apply_inclusion(utterances)
        apply_audio_bounds(utterances, _timings(state))
        state.utterances = utterances

        low_confidence = sum(1 for u in utterances if u.label_confidence < 0.6)
        if low_confidence:
            warnings.append(f"{low_confidence} span(s) classified below 0.6 confidence")

        log.info("segmentation_complete", utterances=len(utterances), included=sum(1 for u in utterances if u.is_included_downstream), used_model=used_model, low_confidence=low_confidence)
        return StageResult(output=state, confidence=1.0 if used_model else 0.5, cost_usd=cost, model_id=model_id, warnings=warnings)
