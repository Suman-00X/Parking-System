"""Rules deciding when a report holds a finding someone must be told about immediately.

Order: propose starting rules from the corpus (seed_candidate_rules) -> write one (author_rule)
-> a radiologist approves it (approve_rule) -> match a report against the live set
(matches, active_rules).
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.text import split_sentences
from radreport.core.types import ActorType, AlertSeverity, PatternType
from radreport.db.models.onboarding import CorpusReport
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.reporting import CriticalFindingRule

log = get_logger(__name__)

#: the starting set: findings that are critical in essentially any lab.
BASELINE_FINDINGS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("PNEUMOTHORAX", "Pneumothorax", ("pneumothorax", "collapsed lung", "tension pneumothorax"), AlertSeverity.RED),
    ("AORTIC_DISSECTION", "Aortic dissection", ("aortic dissection", "dissection flap", "intimal flap"), AlertSeverity.RED),
    ("INTRACRANIAL_HAEMORRHAGE", "Intracranial haemorrhage", ("intracranial haemorrhage", "intracranial hemorrhage", "subarachnoid", "extradural", "subdural haematoma", "subdural hematoma"), AlertSeverity.RED),
    ("PULMONARY_EMBOLISM", "Pulmonary embolism", ("pulmonary embolism", "filling defect in the pulmonary art", "saddle embolus"), AlertSeverity.RED),
    ("FREE_AIR", "Pneumoperitoneum / free intraperitoneal air", ("free air", "pneumoperitoneum", "free intraperitoneal gas"), AlertSeverity.RED),
    ("ECTOPIC_PREGNANCY", "Ectopic pregnancy", ("ectopic pregnancy", "ectopic gestation"), AlertSeverity.RED),
    ("TESTICULAR_TORSION", "Testicular torsion", ("testicular torsion", "torsion of the testis"), AlertSeverity.RED),
    ("BOWEL_OBSTRUCTION", "Bowel obstruction", ("bowel obstruction", "obstructed bowel", "transition point"), AlertSeverity.ORANGE),
    ("ACUTE_INFARCT", "Acute infarct", ("acute infarct", "acute infarction", "hyperacute infarct"), AlertSeverity.ORANGE),
    ("MALIGNANCY_SUSPECTED", "Suspected new malignancy", ("suspicious for malignancy", "likely malignant", "neoplastic process"), AlertSeverity.ORANGE),
)

#: defaults. Red is "somebody is telephoned"; orange is same-working-day.
DEFAULT_SLA_MINUTES: dict[str, int] = {AlertSeverity.RED: 60, AlertSeverity.ORANGE: 240}

#: Language that marks a sentence as urgent in a signed report.
_URGENCY = re.compile(
    r"\b(urgent(?:ly)?|immediate(?:ly)?|critical|emergen(?:t|cy)|"
    r"informed the referring|communicated to|notified|stat)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(no|not|without|absent|negative for|ruled out|free of)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RuleCandidate:
    """A proposed rule. Not a rule until a radiologist signs it."""

    code: str
    finding_label: str
    patterns: tuple[str, ...]
    severity: str
    corpus_mentions: int = 0
    example_sentences: tuple[str, ...] = ()
    is_baseline: bool = True


@dataclass(slots=True)
class SeedResult:
    candidates: list[RuleCandidate] = field(default_factory=list)
    created: list[CriticalFindingRule] = field(default_factory=list)
    lab_specific_phrases: list[tuple[str, int]] = field(default_factory=list)
    """Urgency language this lab uses that no baseline rule covers. The radiologist's prompt for rules nobody thought to ask about."""


def seed_candidate_rules(session: Session, *, tenant_id: uuid.UUID, persist: bool = True, actor_id: uuid.UUID | None = None) -> SeedResult:
    """Propose rules from the baseline set plus this lab's corpus language."""
    result = SeedResult()

    texts = list(session.execute(select(CorpusReport.report_text).where(CorpusReport.tenant_id == tenant_id)).scalars().all())
    mention_counts, examples = _corpus_mentions(texts)

    for code, label, patterns, severity in BASELINE_FINDINGS:
        result.candidates.append(RuleCandidate(code=code, finding_label=label, patterns=patterns, severity=severity, corpus_mentions=mention_counts.get(code, 0), example_sentences=tuple(examples.get(code, ()))))

    result.lab_specific_phrases = _unmatched_urgency_phrases(texts)

    if persist:
        for candidate in result.candidates:
            rule = _upsert_rule(session, tenant_id=tenant_id, candidate=candidate)
            if rule is not None:
                result.created.append(rule)
        session.flush()
        if result.created:
            session.add(AuditLog(tenant_id=tenant_id, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action="critical_rules_seeded", entity_type="critical_finding_rule", entity_id=None, after={"seeded": [r.code for r in result.created], "state": "inactive, awaiting radiologist approval"}))
            session.flush()

    log.info("s6_rules_seeded", tenant_id=str(tenant_id), candidates=len(result.candidates), created=len(result.created), lab_specific_phrases=len(result.lab_specific_phrases))
    return result


def _corpus_mentions(texts: list[str]) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Count affirmative mentions of each baseline finding, with examples."""
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}

    for text in texts:
        lowered = text.lower()
        for code, _label, patterns, _severity in BASELINE_FINDINGS:
            for pattern in patterns:
                if pattern not in lowered:
                    continue
                for sentence in split_sentences(text):
                    if pattern not in sentence.lower():
                        continue
                    if _NEGATION.search(sentence):
                        continue
                    counts[code] += 1
                    bucket = examples.setdefault(code, [])
                    stripped = sentence.strip()
                    if len(bucket) < 3 and stripped not in bucket:
                        bucket.append(stripped)
                break

    return dict(counts), examples


def _unmatched_urgency_phrases(texts: list[str], *, min_count: int = 3) -> list[tuple[str, int]]:
    """Urgent-sounding sentences no baseline pattern explains."""
    known = {p for _c, _l, patterns, _s in BASELINE_FINDINGS for p in patterns}
    counts: Counter[str] = Counter()

    for text in texts:
        for sentence in split_sentences(text):
            stripped = sentence.strip()
            if not stripped or not _URGENCY.search(stripped):
                continue
            lowered = stripped.lower()
            if any(pattern in lowered for pattern in known):
                continue
            counts[" ".join(lowered.split())[:120]] += 1

    return [(phrase, n) for phrase, n in counts.most_common(20) if n >= min_count]


def _upsert_rule(session: Session, *, tenant_id: uuid.UUID, candidate: RuleCandidate) -> CriticalFindingRule | None:
    """Write an inactive rule. Returns None if one already exists."""
    existing = session.execute(select(CriticalFindingRule).where(CriticalFindingRule.tenant_id == tenant_id, CriticalFindingRule.code == candidate.code)).scalar_one_or_none()
    if existing is not None:
        return None

    rule = CriticalFindingRule(
        tenant_id=tenant_id,
        code=candidate.code,
        finding_label=candidate.finding_label,
        pattern_type=PatternType.LEXICAL,
        pattern="|".join(candidate.patterns),
        negation_sensitive=True,
        severity=candidate.severity,
        sla_minutes=DEFAULT_SLA_MINUTES[candidate.severity],
        # Empty, and that is the point: the critical-findings rules stage's gate is a human naming who is called.
        escalation_path=[],
        requires_ack=True,
        approved_by=None,
        is_active=False,
    )
    session.add(rule)
    return rule


def author_rule(session: Session, *, tenant_id: uuid.UUID, code: str, finding_label: str, pattern: str, severity: str, sla_minutes: int, escalation_path: list[dict[str, Any]], authored_by: uuid.UUID, pattern_type: str = PatternType.LEXICAL, negation_sensitive: bool = True, requires_ack: bool = True) -> CriticalFindingRule:
    """Create or update a rule with the clinical commitments filled in."""
    if severity not in AlertSeverity.values():
        raise ValueError(f"unknown alert severity {severity!r}")
    if pattern_type not in PatternType.values():
        raise ValueError(f"unknown pattern type {pattern_type!r}")
    if sla_minutes <= 0:
        raise ValueError("sla_minutes must be positive")
    if not escalation_path:
        raise ValueError("an escalation path is required: a rule with an SLA and nobody to call is a log line, not an alert")

    rule = session.execute(select(CriticalFindingRule).where(CriticalFindingRule.tenant_id == tenant_id, CriticalFindingRule.code == code)).scalar_one_or_none()

    before: dict[str, Any] | None = None
    if rule is None:
        rule = CriticalFindingRule(tenant_id=tenant_id, code=code)
        session.add(rule)
    else:
        before = {"pattern": rule.pattern, "severity": rule.severity, "sla_minutes": rule.sla_minutes, "is_active": rule.is_active}
        # Editing a live rule drops it back to unapproved.
        rule.approved_by = None
        rule.is_active = False

    rule.finding_label = finding_label
    rule.pattern_type = pattern_type
    rule.pattern = pattern
    rule.negation_sensitive = negation_sensitive
    rule.severity = severity
    rule.sla_minutes = sla_minutes
    rule.escalation_path = escalation_path
    rule.requires_ack = requires_ack

    session.add(AuditLog(tenant_id=tenant_id, actor_id=authored_by, actor_type=ActorType.USER, action="critical_rule_authored", entity_type="critical_finding_rule", entity_id=rule.id, before=before, after={"code": code, "severity": severity, "sla_minutes": sla_minutes, "escalation_steps": len(escalation_path)}))
    session.flush()
    return rule


def approve_rule(session: Session, *, tenant_id: uuid.UUID, rule_id: uuid.UUID, approved_by: uuid.UUID, activate: bool = True) -> CriticalFindingRule:
    """The critical-findings rules gate: a radiologist signs a rule and it goes live."""
    rule = session.get(CriticalFindingRule, rule_id)
    if rule is None or rule.tenant_id != tenant_id:
        raise ValueError(f"no critical_finding_rule {rule_id} in this tenant")
    if not rule.escalation_path:
        raise ValueError(f"rule {rule.code!r} has no escalation path; a radiologist must name who is contacted and within what SLA before it can be activated")

    before = {"approved_by": str(rule.approved_by), "is_active": rule.is_active}
    rule.approved_by = approved_by
    rule.is_active = activate

    session.add(AuditLog(tenant_id=tenant_id, actor_id=approved_by, actor_type=ActorType.USER, action="critical_rule_approved", entity_type="critical_finding_rule", entity_id=rule.id, before=before, after={"approved_by": str(approved_by), "is_active": activate, "code": rule.code}))
    session.flush()
    log.info("s6_rule_approved", tenant_id=str(tenant_id), rule_id=str(rule.id), code=rule.code, active=activate)
    return rule


def matches(rule: CriticalFindingRule, transcript: str) -> bool:
    """Would this rule fire on this transcript?"""
    if rule.pattern_type != PatternType.LEXICAL:
        raise NotImplementedError(f"pattern_type {rule.pattern_type!r} is evaluated by the pipeline's critical-findings stage, not by critical-findings rules")

    patterns = [p.strip().lower() for p in rule.pattern.split("|") if p.strip()]
    for sentence in split_sentences(transcript):
        lowered = sentence.lower()
        if not any(pattern in lowered for pattern in patterns):
            continue
        if rule.negation_sensitive and _NEGATION.search(sentence):
            continue
        return True
    return False


def active_rules(session: Session, *, tenant_id: uuid.UUID) -> list[CriticalFindingRule]:
    """Approved and active — what the readiness gate stage's `critical_rules_approved` check counts."""
    return list(session.execute(select(CriticalFindingRule).where(CriticalFindingRule.tenant_id == tenant_id, CriticalFindingRule.is_active.is_(True), CriticalFindingRule.approved_by.isnot(None))).scalars().all())
