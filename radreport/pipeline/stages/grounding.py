"""Stage 11: proves every value in the draft was actually said, by matching it word for word against the transcript.

Order: normalise both sides for comparison (normalise_for_comparison) -> check the quote is
verbatim (quote_is_verbatim) -> mark each value grounded or not (ground_field_values) -> only
grounded values may be printed (renderable).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from radreport.core.logging import get_logger
from radreport.core.types import CheckType, FillSource, Severity
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.state import FieldValue, PipelineState, ProvenanceRef, VerificationFindingState

log = get_logger(__name__)

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class GroundingFailure:
    field_key: str
    reason: str
    quote: str
    char_start: int
    char_end: int


@dataclass(slots=True)
class CoverageReport:
    grounded: int = 0
    ungrounded: int = 0
    required_field_gaps: list[str] = field(default_factory=list)
    orphan_assertions: list[str] = field(default_factory=list)
    failures: list[GroundingFailure] = field(default_factory=list)


def normalise_for_comparison(text: str) -> str:
    """Collapse whitespace and Unicode form — nothing else."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def quote_is_verbatim(transcript: str, ref: ProvenanceRef) -> bool:
    """Does the cited range actually contain the cited quote?"""
    if ref.char_start < 0 or ref.char_end > len(transcript) or ref.char_start >= ref.char_end:
        return False
    cited = normalise_for_comparison(transcript[ref.char_start : ref.char_end])
    return normalise_for_comparison(ref.quote) == cited


class GroundingStage:
    """Never optional in the graph: a draft whose quotes were never checked looks exactly like one that passed."""

    name = "grounding"
    version = "1.0.0"

    def is_idempotent(self) -> bool:
        return True

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if state.transcript is None:
            raise ValueError("grounding runs against a transcript; none is present")

        report = ground_field_values(state)

        for failure in report.failures:
            state.verification.append(VerificationFindingState(check_id="grounding_failed", check_type=CheckType.ROUNDTRIP, severity=Severity.ERROR, message=failure.reason, field_key=failure.field_key, evidence={"quote": failure.quote, "char_start": failure.char_start, "char_end": failure.char_end}))

        if state.routing is not None:
            state.routing.orphan_assertion_count = len(report.orphan_assertions)
            state.routing.required_field_gap_count = len(report.required_field_gaps)

        warnings = [f"{f.field_key}: {f.reason}" for f in report.failures]
        total = report.grounded + report.ungrounded

        log.info("grounding_complete", grounded=report.grounded, ungrounded=report.ungrounded, orphan_assertions=len(report.orphan_assertions), required_gaps=len(report.required_field_gaps))
        return StageResult(output=state, confidence=round(report.grounded / total, 4) if total else 1.0, warnings=warnings)


def ground_field_values(state: PipelineState, *, required_field_keys: frozenset[str] = frozenset()) -> CoverageReport:
    """Verify every value's provenance and compute coverage. Pure."""
    assert state.transcript is not None
    transcript = state.transcript.text
    included = {u.seq for u in state.included_utterances()}
    known = {u.seq for u in state.utterances}
    report = CoverageReport()

    for key, value in state.field_values.items():
        failure = _check_value(key, value, transcript, included, known)
        if failure is None:
            value.is_grounded = True
            report.grounded += 1
            continue

        value.is_grounded = False
        value.is_flagged = True
        if failure.reason not in value.flag_reasons:
            value.flag_reasons.append(failure.reason)
        report.ungrounded += 1
        report.failures.append(failure)

    report.required_field_gaps = sorted(key for key in required_field_keys if key not in state.field_values or not state.field_values[key].is_grounded)
    report.orphan_assertions = _orphan_assertions(state)
    return report


def _check_value(key: str, value: FieldValue, transcript: str, included: set[int], known: set[int]) -> GroundingFailure | None:
    if value.fill_source == FillSource.TEMPLATE_DEFAULT:
        return GroundingFailure(field_key=key, reason=("template_default fill reached grounding: V1 auto-fills nothing by construction "), quote="", char_start=-1, char_end=-1)

    if not value.provenance:
        return GroundingFailure(field_key=key, reason="no provenance span cited; I1 admits no ungrounded values", quote="", char_start=-1, char_end=-1)

    for ref in value.provenance:
        if not quote_is_verbatim(transcript, ref):
            return GroundingFailure(field_key=key, reason=("cited quote does not appear verbatim in the cited character range — the model paraphrased its own evidence"), quote=ref.quote, char_start=ref.char_start, char_end=ref.char_end)
        if ref.utterance_seq is not None and ref.utterance_seq not in included:
            # Two different faults, reported differently: a reviewer chasing "excluded downstream" looks for the retraction that caused it, and finding none when the utterance never existed wastes the one person whose time this check is spending.
            reason = f"cited utterance {ref.utterance_seq} is excluded downstream (retracted self-correction, aside or other speaker)" if ref.utterance_seq in known else (f"cited utterance {ref.utterance_seq} does not exist in this transcript — a dangling provenance reference")
            return GroundingFailure(field_key=key, reason=reason, quote=ref.quote, char_start=ref.char_start, char_end=ref.char_end)
    return None


def _orphan_assertions(state: PipelineState) -> list[str]:
    """Sketch assertions that no field absorbed — the wrong-template signal."""
    if state.sketch is None:
        return []

    absorbed = normalise_for_comparison(" ".join(part for value in state.field_values.values() if value.is_grounded for part in (value.value_text or "", value.value_enum or "") if part)).lower()

    orphans: list[str] = []
    for assertion in state.sketch.assertions:
        probe = normalise_for_comparison(assertion).lower()
        if probe and probe not in absorbed:
            orphans.append(assertion)
    return orphans


def renderable(state: PipelineState) -> dict[str, FieldValue]:
    """The only values compose may read. Ungrounded fields never render (I1)."""
    return {k: v for k, v in state.field_values.items() if v.is_grounded}
