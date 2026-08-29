"""Stage 10: fills in the template's fields from the dictation, asking three times and keeping only answers that quote the audio.

Order: build the prompt per section (build_prompt) -> read each answer (parse_fields) -> keep
what the samples agree on (merge_samples). The most expensive stage, and the one carrying the
most safety checks.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from radreport.adapters.llm.base import LLMClient, LLMRequest
from radreport.adapters.llm.prompt import PromptBundle, VolatileBlock, exemplar_block, schema_block, section_block, system_block
from radreport.adapters.llm.sampling import DEFAULT_K, DEFAULT_TEMPERATURE, sample_k
from radreport.core.logging import get_logger
from radreport.core.types import AssertionStatus, FillSource, Laterality, TaskKey
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.state import FieldValue, PipelineState, ProvenanceRef
from radreport.pipeline.timing import WordTiming, audio_span

log = get_logger(__name__)

SYSTEM_PROMPT = """\
You extract structured fields from a dictated radiology report.

Absolute rules:
- Every value you return MUST cite a verbatim quote from the transcript, with
  its exact character offsets. A value you cannot cite, you do not return.
- Quote exactly. Do not paraphrase, correct grammar, or expand abbreviations
  inside a quote.
- If the radiologist did not address a field, omit it. Do NOT infer a normal
  value from silence. An unmentioned organ is not a normal organ.
- Presence is four-state: present, absent, uncertain, not_assessed. A dictated
  negative is `absent`, not an omission.
- Report laterality exactly as dictated.

Return JSON: {"fields": {"<field_key>": {"value_text": str|null,
"value_enum": str|null, "value_numeric": number|null, "value_unit": str|null,
"assertion_status": str, "laterality": str|null, "quote": str,
"char_start": int, "char_end": int}}}"""


@dataclass(frozen=True, slots=True)
class SectionSpec:
    """One extraction unit: a section and the fields it owns."""

    section: str
    field_keys: tuple[str, ...]
    json_schema: dict[str, Any]
    instruction: str = ""
    pinned_exemplars: tuple[str, ...] = ()
    """**Pinned** only."""


@dataclass(slots=True)
class ExtractionResult:
    values: dict[str, FieldValue] = field(default_factory=dict)
    dropped_uncited: list[str] = field(default_factory=list)
    disagreements: dict[str, float] = field(default_factory=dict)
    cost_usd: float = 0.0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def build_prompt(spec: SectionSpec, transcript: str, glossary: dict[str, str]) -> PromptBundle:
    """Stable system → schema → section instruction → pinned exemplars, then the volatile transcript (the tier ordering)."""
    stable = [system_block(SYSTEM_PROMPT), schema_block(json.dumps(spec.json_schema, sort_keys=True, separators=(",", ":")))]
    if glossary:
        stable.append(section_block("Lab shorthand, already resolved:\n" + "\n".join(f"  {k} = {v}" for k, v in sorted(glossary.items())), label="glossary"))
    if spec.instruction:
        stable.append(section_block(spec.instruction, label=f"section:{spec.section}"))
    if spec.pinned_exemplars:
        stable.append(exemplar_block("\n\n".join(spec.pinned_exemplars), pinned=True))

    return PromptBundle(stable=stable, volatile=[VolatileBlock(text=f"<transcript>\n{transcript}\n</transcript>", label="transcript")])


def parse_fields(text: str, allowed: frozenset[str]) -> dict[str, dict[str, Any]]:
    """Parse one sample's JSON, keeping only fields this section owns."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        log.warning("extraction_response_not_json", length=len(text))
        return {}
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        return {}
    return {k: v for k, v in fields.items() if k in allowed and isinstance(v, dict)}


def _value_signature(item: dict[str, Any]) -> str:
    """What counts as "the same answer" across samples."""
    return json.dumps({"value_text": (item.get("value_text") or "").strip().lower() or None, "value_enum": item.get("value_enum"), "value_numeric": item.get("value_numeric"), "assertion_status": item.get("assertion_status"), "laterality": item.get("laterality")}, sort_keys=True)


def merge_samples(samples: list[dict[str, dict[str, Any]]], transcript: str, timings: list[WordTiming] | None = None) -> ExtractionResult:
    """Majority vote per field; agreement becomes that field's confidence."""
    result = ExtractionResult()
    if not samples:
        return result

    keys = {k for sample in samples for k in sample}
    for key in sorted(keys):
        present = [s[key] for s in samples if key in s]
        counts = Counter(_value_signature(item) for item in present)
        signature, votes = counts.most_common(1)[0]
        # Denominator is k, not len(present): a field two of three samples did not return at all is a weak field, and scoring it on the samples that did return would report it as unanimous.
        agreement = round(votes / len(samples), 4)
        winner = next(item for item in present if _value_signature(item) == signature)

        quote = (winner.get("quote") or "").strip()
        start = winner.get("char_start")
        end = winner.get("char_end")
        if not quote or not isinstance(start, int) or not isinstance(end, int):
            # I1: a value with no citation is dropped here rather than passed on.
            result.dropped_uncited.append(key)
            continue

        audio_start_ms, audio_end_ms, _interpolated = audio_span(timings or [], start, end)
        result.values[key] = FieldValue(
            field_key=key,
            value_text=winner.get("value_text"),
            value_enum=winner.get("value_enum"),
            value_numeric=winner.get("value_numeric"),
            value_unit=winner.get("value_unit"),
            assertion_status=winner.get("assertion_status") or AssertionStatus.NOT_ASSESSED,
            laterality=winner.get("laterality") or Laterality.NA,
            fill_source=FillSource.DICTATED,
            # Grounding (stage 11) decides this. Extraction never asserts that
            # its own citation checks out.
            is_grounded=False,
            confidence=agreement,
            is_flagged=agreement < 1.0,
            flag_reasons=["sample_disagreement"] if agreement < 1.0 else [],
            provenance=[
                ProvenanceRef(
                    char_start=start,
                    char_end=end,
                    # The audio range this quote occupies.
                    audio_start_ms=audio_start_ms,
                    audio_end_ms=audio_end_ms,
                    quote=quote,
                    extraction_confidence=agreement,
                )
            ],
        )
        if agreement < 1.0:
            result.disagreements[key] = agreement

    return result


class ExtractStage:
    """k samples per section, cache-warmed."""

    name = "extract"
    version = "1.0.0"
    task_key = TaskKey.EXTRACTION

    def __init__(self, client: LLMClient, sections: list[SectionSpec], *, k: int = DEFAULT_K, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = 4096) -> None:
        self._client = client
        self._sections = sections
        self._k = k
        self._temperature = temperature
        self._max_tokens = max_tokens

    def is_idempotent(self) -> bool:
        """False: k paid calls per section."""
        return False

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if state.transcript is None:
            raise ValueError("extraction runs on a transcript; none is present")

        from radreport.pipeline.stages.normalise import glossary as build_glossary

        resolved = await ctx.resolve_model(self.task_key)
        model_id = resolved.ref.model_id
        glossary = build_glossary(state.resolutions)

        # The **full** transcript, deliberately.
        transcript = state.transcript.text
        timings = [WordTiming(char_start=t.char_start, char_end=t.char_end, start_ms=t.start_ms, end_ms=t.end_ms) for t in state.word_timings]

        total = ExtractionResult()
        warnings: list[str] = []

        for spec in self._sections:
            prompt = build_prompt(spec, transcript, glossary)
            samples = await sample_k(self._client, LLMRequest(prompt=prompt, max_tokens=self._max_tokens, json_schema=spec.json_schema), model_id=model_id, k=self._k, temperature=self._temperature)
            parsed = [parse_fields(r.text, frozenset(spec.field_keys)) for r in samples.responses]
            merged = merge_samples(parsed, transcript, timings)

            total.values.update(merged.values)
            total.dropped_uncited.extend(merged.dropped_uncited)
            total.disagreements.update(merged.disagreements)
            total.cost_usd += samples.total_cost_usd
            total.cache_read_tokens += samples.cache_read_tokens
            total.cache_write_tokens += samples.cache_write_tokens

            if self._k > 1 and not samples.warm_up_hit:
                warnings.append(f"section {spec.section}: fan-out samples read no cache — check the warm-up ordering")

        state.field_values.update(total.values)

        if total.dropped_uncited:
            warnings.append(f"{len(total.dropped_uncited)} field(s) returned without a citation and were dropped (I1): {', '.join(sorted(total.dropped_uncited)[:5])}")
        if total.disagreements:
            warnings.append(f"{len(total.disagreements)} field(s) had sample disagreement; their confidence is reduced rather than their value discarded")

        confidence = round(sum(v.confidence for v in total.values.values()) / len(total.values), 4) if total.values else 0.0
        log.info("extraction_complete", sections=len(self._sections), k=self._k, fields=len(total.values), dropped_uncited=len(total.dropped_uncited), disagreements=len(total.disagreements), cost_usd=round(total.cost_usd, 6), cache_read_tokens=total.cache_read_tokens)
        return StageResult(output=state, confidence=confidence, cost_usd=total.cost_usd, model_id=model_id, cache_read_tokens=total.cache_read_tokens, cache_write_tokens=total.cache_write_tokens, warnings=warnings)
