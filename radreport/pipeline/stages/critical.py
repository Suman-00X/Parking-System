"""Stage 7: spots findings someone must be told about immediately, and raises the alert.

Order: match the dictation against the lab's rules (detect_alerts, scanned_labels) -> take the
most serious (highest_severity) -> decide whether it skips the normal queue (bypasses_queue).
It runs early on purpose, so an alert still fires on a report that later fails.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass

from radreport.core.logging import get_logger
from radreport.core.text import split_sentences
from radreport.core.types import AlertSeverity, PatternType, UtteranceLabel
from radreport.db.models.reporting import CriticalFindingAlert
from radreport.pipeline.contracts import StageContext, StageResult
from radreport.pipeline.stages.providers import CriticalRuleEntry, KnowledgeProvider
from radreport.pipeline.state import CriticalAlertState, PipelineState, Utterance

log = get_logger(__name__)

_NEGATION = re.compile(
    r"\b(no|not|without|absent|negative for|ruled out|free of|denies|"
    r"unremarkable for)\b",
    re.IGNORECASE,
)
#: Hedges do not suppress an alert; they lower its confidence.
_HEDGE = re.compile(
    r"\b(possible|possibly|probable|probably|suspicious for|query|"
    r"cannot exclude|equivocal|may represent)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Alert:
    rule_id: uuid.UUID
    rule_code: str
    finding_label: str
    severity: str
    sla_minutes: int
    evidence_text: str
    confidence: float
    utterance_seq: int | None
    hedged: bool


class CriticalFindingsStage:
    """Never optional in the graph: a report whose alerting was silently skipped looks exactly like one with nothing to report."""

    name = "critical_findings"
    version = "1.0.0"

    def __init__(self, knowledge: KnowledgeProvider) -> None:
        self._knowledge = knowledge

    def is_idempotent(self) -> bool:
        return True

    async def run(self, state: PipelineState, ctx: StageContext) -> StageResult[PipelineState]:
        if state.transcript is None:
            raise ValueError("critical-findings detection runs on a transcript; none is present")

        knowledge = self._knowledge.for_tenant(state.tenant_id)
        if not knowledge.critical_rules:
            # Not an error here — readiness gate is what blocks a lab from reaching pilot with no rules.
            log.warning("no_active_critical_rules", tenant_id=str(state.tenant_id), detail="this report has no alert path; readiness gates the pilot on it")
            return StageResult(output=state, confidence=0.0, warnings=["no active critical-finding rules for this tenant"])

        alerts = detect_alerts(state.transcript.text, knowledge.critical_rules, utterances=state.utterances)
        state.critical_alerts = [CriticalAlertState(rule_code=a.rule_code, evidence_text=a.evidence_text, confidence=a.confidence, utterance_seq=a.utterance_seq) for a in alerts]

        # The alert row, not just the state.
        now = dt.datetime.now(dt.UTC)
        pending = [CriticalFindingAlert(tenant_id=state.tenant_id, recording_id=state.recording_id, rule_id=alert.rule_id, evidence_text=alert.evidence_text, confidence=alert.confidence, detected_at=now, sla_due_at=now + dt.timedelta(minutes=alert.sla_minutes)) for alert in alerts]

        warnings = [f"{a.severity.upper()} alert {a.rule_code}: {a.evidence_text!r} (SLA {a.sla_minutes} min)" for a in alerts]

        log.info("critical_findings_detected", alerts=len(alerts), red=sum(1 for a in alerts if a.severity == AlertSeverity.RED), rule_codes=[a.rule_code for a in alerts], tenant_id=str(state.tenant_id))
        return StageResult(output=state, confidence=1.0, warnings=warnings, pending_writes=pending)


def detect_alerts(transcript: str, rules: tuple[CriticalRuleEntry, ...], *, utterances: list[Utterance] | None = None) -> list[Alert]:
    """Match every rule against every sentence. Pure and replayable."""
    segments = _segments(transcript, utterances)
    alerts: list[Alert] = []

    for rule in rules:
        if rule.pattern_type != PatternType.LEXICAL:
            # is "lexical first, LLM as a recall net".
            log.info("critical_rule_deferred", rule_code=rule.rule_code, pattern_type=rule.pattern_type, detail="non-lexical rules are evaluated by the LLM recall net")
            continue

        for seq, sentence in segments:
            lowered = sentence.lower()
            hit = next((p for p in rule.patterns if p.lower() in lowered), None)
            if hit is None:
                continue
            if rule.negation_sensitive and _is_negated(sentence, hit):
                continue

            hedged = _HEDGE.search(sentence) is not None
            alerts.append(
                Alert(
                    rule_id=rule.rule_id,
                    rule_code=rule.rule_code,
                    finding_label=rule.finding_label,
                    severity=rule.severity,
                    sla_minutes=rule.sla_minutes,
                    evidence_text=sentence.strip(),
                    # A hedge lowers confidence but never suppresses: "possible
                    # pneumothorax" is precisely the call worth making.
                    confidence=0.6 if hedged else 0.95,
                    utterance_seq=seq,
                    hedged=hedged,
                )
            )
            break  # One alert per rule per report; the first hit is evidence enough.

    return sorted(alerts, key=lambda a: (a.severity != AlertSeverity.RED, a.rule_code))


def _segments(transcript: str, utterances: list[Utterance] | None) -> list[tuple[int | None, str]]:
    """Sentences to scan, with their utterance seq where one is known."""
    if not utterances:
        return [(None, s) for s in split_sentences(transcript) if s.strip()]

    segments: list[tuple[int | None, str]] = []
    for utterance in sorted(utterances, key=lambda u: u.seq):
        if utterance.superseded_by_seq is not None:
            # Retracted by a self-correction. Alerting on the half the
            # radiologist took back is a false alarm with a phone call attached.
            continue
        for sentence in split_sentences(utterance.text):
            if sentence.strip():
                segments.append((utterance.seq, sentence))
    return segments


def _is_negated(sentence: str, pattern: str) -> bool:
    """Is the matched pattern negated *within this sentence*?"""
    lowered = sentence.lower()
    position = lowered.find(pattern.lower())
    if position < 0:
        return False
    return _NEGATION.search(lowered[:position]) is not None


def highest_severity(alerts: list[Alert]) -> str | None:
    if any(a.severity == AlertSeverity.RED for a in alerts):
        return AlertSeverity.RED
    if alerts:
        return AlertSeverity.ORANGE
    return None


def bypasses_queue(alerts: list[Alert]) -> bool:
    """An alert goes out **now**, not when the draft is reviewed."""
    return bool(alerts)


def scanned_labels() -> frozenset[str]:
    """Which utterance labels this stage reads. Everything except retractions."""
    return frozenset(UtteranceLabel.values())
