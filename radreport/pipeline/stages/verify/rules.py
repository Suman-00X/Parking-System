"""Stage 13: the actual checks -- left/right, negation, measurements, auto-fill and schema.

Order: run_checks applies each rule (check_laterality_agrees_with_transcript,
check_negation_agrees_with_assertion, check_measurements_are_plausible,
check_measurement_matches_source, check_nothing_was_auto_filled,
check_ungrounded_values_are_not_renderable, check_enum_values_are_in_schema), then summarise
reduces the findings to a verdict.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from radreport.core.logging import get_logger
from radreport.core.types import AssertionStatus, CheckType, FillSource, Laterality, Severity
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.state import PipelineState, VerificationFindingState

log = get_logger(__name__)

CheckFn = Callable[[PipelineState], list[VerificationFindingState]]

_LATERALITY_WORD = re.compile(r"\b(left|right|bilateral|midline)\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(no|not|without|absent|negative for|free of)\b", re.IGNORECASE)
_NUMBER_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|cm|ml|cc|hu)\b", re.IGNORECASE)

#: Plausible upper bounds, per unit, for a single reported measurement.
_MAX_MEASUREMENT: dict[str, float] = {"mm": 500.0, "cm": 60.0, "ml": 10_000.0, "cc": 10_000.0}


def check_laterality_agrees_with_transcript(state: PipelineState) -> list[VerificationFindingState]:
    """A field claiming `left` whose own quote says `right`."""
    findings: list[VerificationFindingState] = []
    for key, value in state.field_values.items():
        if not value.laterality or value.laterality == Laterality.NA:
            continue
        quoted = " ".join(ref.quote for ref in value.provenance)
        if not quoted:
            continue
        said = {m.group(0).lower() for m in _LATERALITY_WORD.finditer(quoted)}
        if said and value.laterality.lower() not in said:
            findings.append(VerificationFindingState(check_id="laterality_disagrees_with_source", check_type=CheckType.RULE, severity=Severity.BLOCK, message=(f"field records laterality {value.laterality!r} but its cited quote says {', '.join(sorted(said))}"), field_key=key, evidence={"quote": quoted, "recorded": value.laterality}))
    return findings


def check_negation_agrees_with_assertion(state: PipelineState) -> list[VerificationFindingState]:
    """`present` asserted from a quote that negates it."""
    findings: list[VerificationFindingState] = []
    for key, value in state.field_values.items():
        if value.assertion_status != AssertionStatus.PRESENT:
            continue
        quoted = " ".join(ref.quote for ref in value.provenance)
        if quoted and _NEGATION.search(quoted):
            findings.append(VerificationFindingState(check_id="assertion_contradicts_source", check_type=CheckType.RULE, severity=Severity.BLOCK, message=("field asserts the finding is present, but its cited quote contains a negation"), field_key=key, evidence={"quote": quoted}))
    return findings


def check_measurements_are_plausible(state: PipelineState) -> list[VerificationFindingState]:
    """A measurement far outside any anatomical range — a transcription error."""
    findings: list[VerificationFindingState] = []
    for key, value in state.field_values.items():
        if value.value_numeric is None or not value.value_unit:
            continue
        limit = _MAX_MEASUREMENT.get(value.value_unit.lower())
        if limit is not None and value.value_numeric > limit:
            findings.append(VerificationFindingState(check_id="measurement_out_of_range", check_type=CheckType.RULE, severity=Severity.WARN, message=(f"{value.value_numeric} {value.value_unit} is outside the plausible range — check for a decimal or unit error"), field_key=key, evidence={"value": value.value_numeric, "unit": value.value_unit}))
    return findings


def check_measurement_matches_source(state: PipelineState) -> list[VerificationFindingState]:
    """The number in the field is the number in the quote."""
    findings: list[VerificationFindingState] = []
    for key, value in state.field_values.items():
        if value.value_numeric is None:
            continue
        quoted = " ".join(ref.quote for ref in value.provenance)
        if not quoted:
            continue
        numbers = {float(m.group(1)) for m in _NUMBER_UNIT.finditer(quoted)}
        numbers |= {float(n) for n in re.findall(r"\b\d+(?:\.\d+)?\b", quoted)}
        if numbers and value.value_numeric not in numbers:
            findings.append(VerificationFindingState(check_id="measurement_not_in_source", check_type=CheckType.ROUNDTRIP, severity=Severity.ERROR, message=(f"field records {value.value_numeric} but its cited quote contains {sorted(numbers)}"), field_key=key, evidence={"recorded": value.value_numeric, "in_quote": sorted(numbers)}))
    return findings


def check_nothing_was_auto_filled(state: PipelineState) -> list[VerificationFindingState]:
    """V1 auto-fills nothing, checked here rather than trusted."""
    findings: list[VerificationFindingState] = []
    for key, value in state.field_values.items():
        if value.fill_source in (FillSource.TEMPLATE_DEFAULT, FillSource.BLANKET_NORMAL):
            findings.append(VerificationFindingState(check_id="auto_fill_in_v1", check_type=CheckType.RULE, severity=Severity.BLOCK, message=(f"field was filled from {value.fill_source!r}; V1 auto-fills nothing — this is a regression, not a finding"), field_key=key, evidence={"fill_source": value.fill_source}))
    return findings


def check_ungrounded_values_are_not_renderable(state: PipelineState) -> list[VerificationFindingState]:
    """I1's backstop: nothing ungrounded may carry a value into compose."""
    findings: list[VerificationFindingState] = []
    for key, value in state.field_values.items():
        has_content = any((value.value_text, value.value_enum, value.value_numeric is not None))
        if has_content and not value.is_grounded and not value.is_flagged:
            findings.append(VerificationFindingState(check_id="ungrounded_value_unflagged", check_type=CheckType.SCHEMA, severity=Severity.BLOCK, message="ungrounded value carries content but is not flagged (I1)", field_key=key))
    return findings


def check_enum_values_are_in_schema(state: PipelineState, *, enum_options: dict[str, tuple[str, ...]]) -> list[VerificationFindingState]:
    """A value outside its field's declared enum."""
    findings: list[VerificationFindingState] = []
    if not enum_options:
        return findings
    for key, value in state.field_values.items():
        options = enum_options.get(key)
        if options and value.value_enum and value.value_enum not in options:
            findings.append(VerificationFindingState(check_id="enum_value_out_of_schema", check_type=CheckType.SCHEMA, severity=Severity.ERROR, message=f"{value.value_enum!r} is not one of {list(options)}", field_key=key, evidence={"value": value.value_enum, "allowed": list(options)}))
    return findings


#: The checks that need only the state.
DETERMINISTIC_CHECKS: tuple[CheckFn, ...] = (check_laterality_agrees_with_transcript, check_negation_agrees_with_assertion, check_measurement_matches_source, check_measurements_are_plausible, check_nothing_was_auto_filled, check_ungrounded_values_are_not_renderable)


def run_checks(state: PipelineState, *, enum_options: dict[str, tuple[str, ...]] | None = None) -> list[VerificationFindingState]:
    """Every deterministic check, in declared order. Pure and replayable."""
    findings: list[VerificationFindingState] = []
    for check in DETERMINISTIC_CHECKS:
        findings.extend(check(state))
    findings.extend(check_enum_values_are_in_schema(state, enum_options=enum_options or {}))
    return findings


@dataclass(frozen=True, slots=True)
class VerificationSummary:
    blocking: int
    errors: int
    warnings: int

    @property
    def may_enter_queue(self) -> bool:
        return self.blocking == 0


def summarise(findings: list[VerificationFindingState]) -> VerificationSummary:
    return VerificationSummary(blocking=sum(1 for f in findings if f.severity == Severity.BLOCK), errors=sum(1 for f in findings if f.severity == Severity.ERROR), warnings=sum(1 for f in findings if f.severity == Severity.WARN))


class VerifyStage:
    """Deterministic rules only at V1."""

    name = "verify"
    version = "1.0.0"

    def __init__(self, *, enum_options: dict[str, tuple[str, ...]] | None = None) -> None:
        self._enum_options = enum_options or {}

    def is_idempotent(self) -> bool:
        return True

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        findings = run_checks(state, enum_options=self._enum_options)
        state.verification.extend(findings)

        for finding in findings:
            if finding.field_key and finding.field_key in state.field_values:
                value = state.field_values[finding.field_key]
                value.is_flagged = True
                if finding.check_id not in value.flag_reasons:
                    value.flag_reasons.append(finding.check_id)

        summary = summarise(state.verification)
        log.info("verification_complete", blocking=summary.blocking, errors=summary.errors, warnings=summary.warnings, may_enter_queue=summary.may_enter_queue)
        return StageResult(output=state, confidence=0.0 if summary.blocking else 1.0, warnings=[f"{f.severity}: {f.check_id} — {f.message}" for f in findings])
