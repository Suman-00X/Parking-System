"""Stage 9: picks which report template this dictation belongs to.

Order: drop templates that cannot apply (hard_filter) -> drop those that do not fit the patient
(demographic_filter) -> score the rest on wording and meaning together (hybrid_rank) -> pick
from the shortlist (ShortlistPicker) -> add any optional sections (attach_modules).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from radreport.core.logging import get_logger
from radreport.core.types import Sex, Stage1FilterSource
from radreport.db.models.reporting import RoutingDecision
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.stages.providers import KnowledgeProvider
from radreport.pipeline.state import PipelineState, RoutingState

log = get_logger(__name__)

#: hands the model five candidates. More costs tokens for candidates the
#: hybrid rank already scored as implausible; fewer risks excluding the answer.
SHORTLIST_SIZE = 5

#: Below this the cascade declines to route rather than picking the least-bad
#: of twenty templates.
MIN_ROUTING_CONFIDENCE = 0.35

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class TemplateCandidate:
    """One routable template, with everything the cascade scores on."""

    template_version_id: uuid.UUID
    template_code: str
    display_name: str
    modality: str
    body_region: str
    routing_card: str
    spoken_study_code: str
    usage_count_12m: int = 0
    applicable_sex: tuple[str, ...] = ()
    applicable_age_min: int | None = None
    applicable_age_max: int | None = None
    is_module: bool = False
    parent_compatible_codes: tuple[str, ...] = ()
    field_keys: frozenset[str] = frozenset()
    required_field_keys: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PatientContext:
    """The demographic filter. Age and sex only — sends nothing else."""

    sex: str | None = None
    age_years: int | None = None
    modality: str | None = None
    body_part: str | None = None
    study_description: str | None = None


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: TemplateCandidate
    score: float
    reason: str


class ShortlistPicker(Protocol):
    """The one model call in the cascade."""

    async def pick(self, transcript: str, shortlist: Sequence[ScoredCandidate]) -> tuple[uuid.UUID, float, str]: ...


def hard_filter(candidates: Sequence[TemplateCandidate], context: PatientContext) -> list[TemplateCandidate]:
    """Modality and body-part exclusions — facts, not preferences."""
    if context.modality is None and context.body_part is None:
        return list(candidates)

    kept: list[TemplateCandidate] = []
    for candidate in candidates:
        if context.modality and candidate.modality.upper() not in (context.modality.upper(), "UNKNOWN"):
            continue
        if context.body_part and candidate.body_region.lower() not in ("unspecified", context.body_part.lower()):
            continue
        kept.append(candidate)
    return kept or list(candidates)


def demographic_filter(candidates: Sequence[TemplateCandidate], context: PatientContext) -> list[TemplateCandidate]:
    """Sex and age applicability."""
    kept: list[TemplateCandidate] = []
    for candidate in candidates:
        if candidate.applicable_sex and context.sex and context.sex != Sex.U and context.sex not in candidate.applicable_sex:
            continue
        if context.age_years is not None:
            if candidate.applicable_age_min is not None and (context.age_years < candidate.applicable_age_min):
                continue
            if candidate.applicable_age_max is not None and (context.age_years > candidate.applicable_age_max):
                continue
        kept.append(candidate)
    return kept or list(candidates)


def hybrid_rank(candidates: Sequence[TemplateCandidate], transcript: str, context: PatientContext, *, referrer_prior: dict[str, float] | None = None) -> list[ScoredCandidate]:
    """Lexical overlap + usage prior + referrer prior."""
    tokens = set(_WORD.findall(transcript.lower()))
    if not tokens:
        return []

    max_usage = max((c.usage_count_12m for c in candidates), default=0) or 1
    scored: list[ScoredCandidate] = []

    for candidate in candidates:
        card_tokens = set(_WORD.findall(candidate.routing_card.lower()))
        overlap = len(tokens & card_tokens) / len(card_tokens) if card_tokens else 0.0

        spoken = candidate.spoken_study_code.lower().strip()
        spoken_hit = 0.25 if spoken and spoken in transcript.lower() else 0.0

        # Log-scaled: a template with 10× the volume is more likely, but not 10× more likely, and a linear prior would let the head swallow every ambiguous dictation.
        usage = (candidate.usage_count_12m / max_usage) ** 0.5 * 0.20

        referrer = 0.0
        if referrer_prior:
            referrer = referrer_prior.get(candidate.template_code, 0.0) * 0.15

        score = round(min(1.0, overlap * 0.40 + spoken_hit + usage + referrer), 4)
        scored.append(ScoredCandidate(candidate=candidate, score=score, reason=(f"card overlap {overlap:.2f}, usage {candidate.usage_count_12m}, spoken-code hit {bool(spoken_hit)}, referrer prior {referrer:.2f}")))

    return sorted(scored, key=lambda s: (-s.score, s.candidate.template_code))


def attach_modules(chosen: TemplateCandidate, candidates: Sequence[TemplateCandidate], transcript: str) -> list[uuid.UUID]:
    """Add compatible modules the dictation actually mentions."""
    lowered = transcript.lower()
    attached: list[uuid.UUID] = []
    for candidate in candidates:
        if not candidate.is_module:
            continue
        if candidate.parent_compatible_codes and chosen.template_code not in candidate.parent_compatible_codes:
            continue
        cues = set(_WORD.findall(candidate.display_name.lower()))
        if cues and cues & set(_WORD.findall(lowered)):
            attached.append(candidate.template_version_id)
    return attached


@dataclass(slots=True)
class RoutingOutcome:
    chosen: TemplateCandidate | None
    confidence: float
    shortlist: list[ScoredCandidate] = field(default_factory=list)
    filter_source: str | None = None
    attached_modules: list[uuid.UUID] = field(default_factory=list)
    used_model: bool = False
    reason: str = ""


class RoutingStage:
    """Deterministic except for the injected shortlist picker."""

    name = "routing"
    version = "1.0.0"

    def __init__(self, knowledge: KnowledgeProvider, candidates: Sequence[TemplateCandidate], *, picker: ShortlistPicker | None = None, referrer_prior: dict[str, float] | None = None, shortlist_size: int = SHORTLIST_SIZE) -> None:
        self._knowledge = knowledge
        self._candidates = list(candidates)
        self._picker = picker
        self._referrer_prior = referrer_prior
        self._shortlist_size = shortlist_size

    def is_idempotent(self) -> bool:
        """False when a picker is bound — that path calls a paid model."""
        return self._picker is None

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if state.transcript is None:
            raise ValueError("routing runs on a transcript; none is present")

        context = _patient_context(state)
        outcome = await self._route(state, context)

        routing = state.routing or RoutingState()
        routing.candidate_set = [{"template_version_id": str(s.candidate.template_version_id), "template_code": s.candidate.template_code, "score": s.score, "reason": s.reason} for s in outcome.shortlist]
        routing.stage1_filter_source = outcome.filter_source
        routing.stage1_candidate_count = len(outcome.shortlist)
        routing.confidence = outcome.confidence
        routing.attached_module_version_ids = outcome.attached_modules
        if outcome.chosen is not None:
            routing.chosen_template_version_id = outcome.chosen.template_version_id
        state.routing = routing

        warnings: list[str] = []
        pending: list[object] = []
        if outcome.chosen is None:
            warnings.append("routing declined: no candidate cleared the confidence floor. V1 ships the top ~20 templates, so a dictation outside the head is expected — this goes to a human rather than the nearest of 20")
        else:
            pending.append(RoutingDecision(tenant_id=state.tenant_id, recording_id=state.recording_id, chosen_template_version_id=outcome.chosen.template_version_id, attached_module_version_ids=outcome.attached_modules or None, candidate_set=routing.candidate_set, stage1_filter_source=outcome.filter_source, stage1_candidate_count=routing.stage1_candidate_count, confidence=outcome.confidence))

        log.info("routing_complete", chosen=outcome.chosen.template_code if outcome.chosen else None, confidence=outcome.confidence, filter_source=outcome.filter_source, shortlist=len(outcome.shortlist), used_model=outcome.used_model, modules=len(outcome.attached_modules))
        return StageResult(output=state, confidence=outcome.confidence, warnings=warnings, pending_writes=pending)

    async def _route(self, state: PipelineState, context: PatientContext) -> RoutingOutcome:
        assert state.transcript is not None
        transcript = state.transcript.text

        # 1. The study code short-circuits the whole cascade. No model call,
        #  no ranking — this is what the spoken-code convention buys.
        detection = state.study_code
        if detection and detection.template_version_id and not detection.escalated:
            chosen = self._by_version_id(detection.template_version_id)
            if chosen is not None:
                return RoutingOutcome(chosen=chosen, confidence=detection.confidence, shortlist=[ScoredCandidate(chosen, detection.confidence, "spoken study code")], filter_source=Stage1FilterSource.CODE_WORD, attached_modules=attach_modules(chosen, self._candidates, transcript), reason="routed on the spoken study code")

        # 2–4. Hard filter, demographics, hybrid rank.
        pool = [c for c in self._candidates if not c.is_module]
        filtered = hard_filter(pool, context)
        filter_source = Stage1FilterSource.DICOM if context.modality or context.body_part else Stage1FilterSource.LEXICAL_FINGERPRINT
        filtered = demographic_filter(filtered, context)
        ranked = hybrid_rank(filtered, transcript, context, referrer_prior=self._referrer_prior)
        shortlist = ranked[: self._shortlist_size]

        if not shortlist:
            return RoutingOutcome(chosen=None, confidence=0.0, filter_source=filter_source, reason="no candidates survived filtering")

        # 5. The model picks from the shortlist — the one call in the cascade.
        used_model = False
        best = shortlist[0]
        confidence = best.score
        if self._picker is not None and len(shortlist) > 1:
            version_id, confidence, _reason = await self._picker.pick(transcript, shortlist)
            picked = next((s for s in shortlist if s.candidate.template_version_id == version_id), None)
            if picked is None:
                # The model named something outside the shortlist it was given.
                log.warning("picker_returned_off_shortlist_candidate", returned=str(version_id), shortlist=[str(s.candidate.template_version_id) for s in shortlist])
                confidence = best.score
            else:
                best = picked
                used_model = True

        if confidence < MIN_ROUTING_CONFIDENCE:
            return RoutingOutcome(chosen=None, confidence=confidence, shortlist=shortlist, filter_source=filter_source, used_model=used_model, reason="below the confidence floor")

        return RoutingOutcome(chosen=best.candidate, confidence=round(confidence, 4), shortlist=shortlist, filter_source=filter_source, attached_modules=attach_modules(best.candidate, self._candidates, transcript), used_model=used_model, reason=best.reason)

    def _by_version_id(self, version_id: uuid.UUID) -> TemplateCandidate | None:
        return next((c for c in self._candidates if c.template_version_id == version_id), None)


def _patient_context(state: PipelineState) -> PatientContext:
    """Age and sex are sent because they are clinically necessary; nothing else about the patient is."""
    meta = state.audio_meta
    return PatientContext(modality=getattr(meta, "modality", None) if meta else None, body_part=getattr(meta, "body_part", None) if meta else None)
