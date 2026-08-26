"""Stage 2c: fixes words the engine misheard, using the lab's own vocabulary and how those words sound.

Order: correct_transcript walks the text and applies each Correction, returning a
CorrectionResult.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from radreport.core.logging import get_logger
from radreport.core.types import TranscriptStage
from radreport.knowledge.phonetics import phonetic_distance
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.stages.providers import KnowledgeProvider, LexiconEntry
from radreport.pipeline.state import PipelineState

log = get_logger(__name__)

#: A surface must be at least this close to a canonical form to be corrected.
MAX_CORRECTION_DISTANCE = 0.10

#: How many words a variant may span. Mined variants are short by nature.
MAX_VARIANT_WORDS = 3

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


@dataclass(frozen=True, slots=True)
class Correction:
    char_start: int
    char_end: int
    before: str
    after: str
    source_variant: str
    speaker_specific: bool = False


@dataclass(slots=True)
class CorrectionResult:
    text: str
    corrections: list[Correction] = field(default_factory=list)
    skipped_ambiguous: list[str] = field(default_factory=list)
    """Surfaces close to two different canonical forms. Left for stage 3's margin guard to escalate rather than corrected on a coin flip."""


def _variant_index(lexicon: tuple[LexiconEntry, ...], speaker_variants: dict[str, str] | None = None) -> dict[str, list[str]]:
    """`lowered variant -> [canonical forms that claim it]`."""
    index: dict[str, list[str]] = {}
    for entry in lexicon:
        for variant in entry.surface_variants:
            key = variant.strip().lower()
            if key and key != entry.canonical_form.lower():
                index.setdefault(key, []).append(entry.canonical_form)
    for variant, canonical in (speaker_variants or {}).items():
        index.setdefault(variant.strip().lower(), []).append(canonical)
    return index


def correct_transcript(text: str, lexicon: tuple[LexiconEntry, ...], *, speaker_variants: dict[str, str] | None = None, max_distance: float = MAX_CORRECTION_DISTANCE) -> CorrectionResult:
    """Apply mined variant corrections. Pure and order-stable."""
    result = CorrectionResult(text=text)
    index = _variant_index(lexicon, speaker_variants)
    if not index:
        return result

    tokens = [(m.start(), m.end(), m.group(0)) for m in _WORD.finditer(text)]
    claimed_until = 0
    pieces: list[str] = []
    cursor = 0
    i = 0

    while i < len(tokens):
        start, _end, _token = tokens[i]
        if start < claimed_until:
            i += 1
            continue

        match: tuple[int, int, str, str, str] | None = None
        for width in range(min(MAX_VARIANT_WORDS, len(tokens) - i), 0, -1):
            span_start = tokens[i][0]
            span_end = tokens[i + width - 1][1]
            surface = text[span_start:span_end]
            canonicals = index.get(surface.lower())
            if not canonicals:
                continue

            unique = sorted(set(canonicals))
            if len(unique) > 1:
                # Two terms claim this surface. Correcting either way is the
                #  failure; stage 3's margin guard escalates it instead.
                result.skipped_ambiguous.append(surface)
                break

            canonical = unique[0]
            if phonetic_distance(surface, canonical) > max_distance:
                continue
            match = (span_start, span_end, surface, canonical, surface.lower())
            break

        if match is None:
            i += 1
            continue

        span_start, span_end, surface, canonical, variant = match
        pieces.append(text[cursor:span_start])
        pieces.append(canonical)
        cursor = span_end
        claimed_until = span_end
        result.corrections.append(Correction(char_start=span_start, char_end=span_end, before=surface, after=canonical, source_variant=variant, speaker_specific=bool(speaker_variants and variant in {k.lower() for k in speaker_variants})))
        i += 1

    pieces.append(text[cursor:])
    result.text = "".join(pieces)
    return result


class PostCorrectionStage:
    """Deterministic; the only stage that rewrites the transcript."""

    name = "post_correction"
    version = "1.0.0"

    def __init__(self, knowledge: KnowledgeProvider, *, max_distance: float = MAX_CORRECTION_DISTANCE, speaker_variants: dict[str, str] | None = None) -> None:
        self._knowledge = knowledge
        self._max_distance = max_distance
        self._speaker_variants = speaker_variants

    def is_idempotent(self) -> bool:
        return True

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if state.transcript is None:
            raise ValueError("post-correction runs on a transcript; none is present")
        if state.utterances or state.resolutions:
            # A guard rather than a comment: by the time utterances exist,
            # offsets are in play and rewriting would invalidate them.
            raise ValueError("post_correction must run before segmentation or normalisation; rewriting the transcript after an offset has been recorded invalidates every provenance span (I1)")

        knowledge = self._knowledge.for_tenant(state.tenant_id)
        result = correct_transcript(state.transcript.text, knowledge.lexicon, speaker_variants=self._speaker_variants, max_distance=self._max_distance)

        state.transcript.text = result.text
        state.transcript.stage = TranscriptStage.NORMALISED

        warnings: list[str] = []
        if result.skipped_ambiguous:
            warnings.append(f"{len(result.skipped_ambiguous)} surface(s) claimed by two terms were left uncorrected for the margin guard: {', '.join(sorted(set(result.skipped_ambiguous))[:5])}")

        log.info("post_correction_complete", corrections=len(result.corrections), speaker_specific=sum(1 for c in result.corrections if c.speaker_specific), skipped_ambiguous=len(result.skipped_ambiguous))
        return StageResult(output=state, confidence=1.0, warnings=warnings)
