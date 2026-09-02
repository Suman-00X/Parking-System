"""Supplies each stage the lab-specific knowledge it needs -- vocabulary, spoken codes, urgent-finding rules -- without giving it database access.

Order: load everything for one lab once (load_tenant_knowledge) into a TenantKnowledge snapshot,
which stages then read through KnowledgeProvider (StaticKnowledgeProvider in tests).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.autonomy.release import AutonomyGrant
from radreport.core.types import CollisionResolution, PatternType, TermType
from radreport.db.models.knowledge import AutonomyClass, LexiconSet, LexiconSurfaceVariant, LexiconTerm, Template, TemplateField, TemplateVersion
from radreport.db.models.onboarding import CollisionAuditFinding
from radreport.db.models.reporting import CriticalFindingRule


@dataclass(frozen=True, slots=True)
class LexiconEntry:
    canonical_form: str
    term_type: str
    phonetic_key_primary: str
    short_form: str | None = None
    phonetic_key_secondary: str | None = None
    is_ambiguous: bool = False
    expansion_policy: str = "per_template"
    surface_variants: tuple[str, ...] = ()
    """What verbatim annotation mined from real transcripts. The correction targets."""


@dataclass(frozen=True, slots=True)
class StudyCodeEntry:
    template_version_id: uuid.UUID
    template_code: str
    spoken_study_code: str
    phonetic_key: str
    variants: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CriticalRuleEntry:
    rule_id: uuid.UUID
    """Needed to persist a `critical_finding_alert`: the alert row references the rule that fired, and the SLA clock is read off it."""

    rule_code: str
    finding_label: str
    pattern_type: str
    patterns: tuple[str, ...]
    negation_sensitive: bool
    severity: str
    sla_minutes: int


@dataclass(frozen=True, slots=True)
class TenantKnowledge:
    """Everything the deterministic stages read, snapshotted per run."""

    tenant_id: uuid.UUID
    lexicon: tuple[LexiconEntry, ...] = ()
    study_codes: tuple[StudyCodeEntry, ...] = ()
    critical_rules: tuple[CriticalRuleEntry, ...] = ()
    template_fields: dict[uuid.UUID, dict[str, uuid.UUID]] = field(default_factory=dict)
    """`template_version_id -> {field_key: template_field_id}`."""

    autonomy: dict[uuid.UUID, AutonomyGrant] = field(default_factory=dict)
    """`template_version_id -> its template's autonomy class`."""

    unresolved_blocking_collisions: tuple[tuple[str, str], ...] = field(default=())
    """Pairs still open in `collision_audit_finding`."""

    def code_words(self) -> tuple[LexiconEntry, ...]:
        return tuple(e for e in self.lexicon if e.term_type in (TermType.CODE_WORD, TermType.ABBREVIATION))


class KnowledgeProvider(Protocol):
    """How a stage asks for this lab's configuration."""

    def for_tenant(self, tenant_id: uuid.UUID) -> TenantKnowledge: ...


@dataclass(slots=True)
class StaticKnowledgeProvider:
    """A fixed snapshot — used by tests and by a single-run orchestration."""

    knowledge: TenantKnowledge

    def for_tenant(self, tenant_id: uuid.UUID) -> TenantKnowledge:
        if tenant_id != self.knowledge.tenant_id:
            raise ValueError(f"knowledge snapshot is for tenant {self.knowledge.tenant_id}, asked for {tenant_id} — a stage must never read another lab's lexicon")
        return self.knowledge


def load_tenant_knowledge(session: Session, tenant_id: uuid.UUID) -> TenantKnowledge:
    """Snapshot a lab's onboarding output for a pipeline run."""
    variants: dict[uuid.UUID, list[str]] = {}
    for term_id, surface in session.execute(select(LexiconSurfaceVariant.lexicon_term_id, LexiconSurfaceVariant.surface_text).join(LexiconTerm, LexiconTerm.id == LexiconSurfaceVariant.lexicon_term_id).join(LexiconSet, LexiconSet.id == LexiconTerm.lexicon_set_id).where(LexiconSet.tenant_id == tenant_id).order_by(LexiconSurfaceVariant.observed_count.desc())).all():
        variants.setdefault(term_id, []).append(surface)

    lexicon = tuple(LexiconEntry(canonical_form=term.canonical_form, term_type=term.term_type, phonetic_key_primary=term.phonetic_key_primary, short_form=term.short_form, phonetic_key_secondary=term.phonetic_key_secondary, is_ambiguous=term.is_ambiguous, expansion_policy=term.expansion_policy, surface_variants=tuple(variants.get(term.id, ()))) for term in session.execute(select(LexiconTerm).join(LexiconSet, LexiconSet.id == LexiconTerm.lexicon_set_id).where(LexiconSet.tenant_id == tenant_id)).scalars().all())

    study_codes = tuple(StudyCodeEntry(template_version_id=version.id, template_code=code, spoken_study_code=version.spoken_study_code, phonetic_key=version.spoken_study_code_phonetic, variants=tuple(version.spoken_study_code_variants or ())) for version, code in session.execute(select(TemplateVersion, Template.code).join(Template, Template.id == TemplateVersion.template_id).where(TemplateVersion.tenant_id == tenant_id, TemplateVersion.is_current.is_(True), Template.is_active.is_(True))).all())

    critical_rules = tuple(CriticalRuleEntry(rule_id=rule.id, rule_code=rule.code, finding_label=rule.finding_label, pattern_type=rule.pattern_type, patterns=tuple(p.strip() for p in rule.pattern.split("|") if p.strip()) if rule.pattern_type == PatternType.LEXICAL else (rule.pattern,), negation_sensitive=rule.negation_sensitive, severity=rule.severity, sla_minutes=rule.sla_minutes) for rule in session.execute(select(CriticalFindingRule).where(CriticalFindingRule.tenant_id == tenant_id, CriticalFindingRule.is_active.is_(True), CriticalFindingRule.approved_by.isnot(None))).scalars().all())

    blocking = tuple((a, b) for a, b in session.execute(select(CollisionAuditFinding.label_a, CollisionAuditFinding.label_b).where(CollisionAuditFinding.tenant_id == tenant_id, CollisionAuditFinding.severity == "block", CollisionAuditFinding.resolution == CollisionResolution.PENDING)).all())

    autonomy = {version_id: AutonomyGrant(class_id=class_id, class_code=class_code, status=status) for version_id, class_id, class_code, status in session.execute(select(TemplateVersion.id, AutonomyClass.id, AutonomyClass.code, AutonomyClass.status).join(Template, Template.id == TemplateVersion.template_id).join(AutonomyClass, AutonomyClass.id == Template.autonomy_class_id).where(TemplateVersion.tenant_id == tenant_id, TemplateVersion.is_current.is_(True))).all()}

    template_fields: dict[uuid.UUID, dict[str, uuid.UUID]] = {}
    for version_id, field_key, field_id in session.execute(select(TemplateField.template_version_id, TemplateField.field_key, TemplateField.id).join(TemplateVersion, TemplateVersion.id == TemplateField.template_version_id).where(TemplateField.tenant_id == tenant_id, TemplateVersion.is_current.is_(True))).all():
        template_fields.setdefault(version_id, {})[field_key] = field_id

    return TenantKnowledge(tenant_id=tenant_id, lexicon=lexicon, study_codes=study_codes, critical_rules=critical_rules, template_fields=template_fields, autonomy=autonomy, unresolved_blocking_collisions=blocking)
