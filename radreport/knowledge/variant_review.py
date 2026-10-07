"""Decides what happens to a candidate match between a heard phrase and a lexicon term: use it, ask a radiologist, or hide it.

Order: score a match (match_confidence: the best of synonym similarity and a blend of sound-alike
and spelling distance) -> place the lab in a threshold arm (threshold_arm; arm B only while the
experiment runs) -> read the thresholds for the term's type (thresholds) -> classify (decide:
auto_approved above the auto threshold, pending above the review threshold, hidden below) -> record a
radiologist's answer, counting it as an override when it reverses an automatic approval (record_review)
-> list what waits, or what was let through for a spot check (queue) -> compare the arms (arm_stats).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, func, literal_column, select
from sqlalchemy.orm import Session

from radreport.core import system_config
from radreport.core.types import ActorType, TermType
from radreport.db.models.knowledge import USED_VARIANTS, LexiconSurfaceVariant
from radreport.db.models.orchestration import AuditLog
from radreport.knowledge.phonetics import normalised_levenshtein, phonetic_distance, strip_non_alpha
from radreport.knowledge.synonyms import semantic_similarity

USED = USED_VARIANTS


def match_confidence(heard: str, term: str) -> float:
    """0-1: 1.0 for the same words; synonyms at 0.95; otherwise sound and spelling distance blended."""
    meaning = semantic_similarity(heard, term)
    if meaning:
        return meaning
    sound = phonetic_distance(heard, term)
    spelling = normalised_levenshtein(strip_non_alpha(heard), strip_non_alpha(term))
    return round(max(0.0, 1.0 - 0.5 * sound - 0.5 * spelling), 4)


def threshold_arm(session: Session, tenant_id: uuid.UUID) -> str:
    """A or B; a lab's arm is fixed by its id, so it never flips between runs."""
    if not int(system_config.resolve(session, "lexicon.ab_test", tenant_id=tenant_id).value):
        return "A"
    return "B" if hashlib.sha256(str(tenant_id).encode()).digest()[0] % 2 else "A"


@dataclass(frozen=True, slots=True)
class Thresholds:
    auto_approve_above: float
    review_above: float
    arm: str


def thresholds(session: Session, tenant_id: uuid.UUID, term_type: str) -> Thresholds:
    arm = threshold_arm(session, tenant_id)
    specific = {TermType.ABBREVIATION: "lexicon.auto_approve_above_abbreviation", TermType.CODE_WORD: "lexicon.auto_approve_above_code_word"}.get(term_type)
    auto = float(system_config.resolve(session, specific or "lexicon.auto_approve_above", tenant_id=tenant_id).value)
    if arm == "B":
        # Arm B loosens only the general threshold; abbreviations and code words keep their strict one in both arms.
        auto = auto if specific else float(system_config.resolve(session, "lexicon.arm_b_auto_approve_above", tenant_id=tenant_id).value)
    review = float(system_config.resolve(session, "lexicon.review_above", tenant_id=tenant_id).value)
    return Thresholds(auto_approve_above=auto, review_above=min(review, auto), arm=arm)


def decide(confidence: float, limits: Thresholds) -> str | None:
    """auto_approved, pending, or None for hidden."""
    if confidence > limits.auto_approve_above:
        return "auto_approved"
    if confidence >= limits.review_above:
        return "pending"
    return None


def queue(tenant_id: uuid.UUID, status: str = "pending") -> Any:
    """Variants in one review status with the term each would map to; least sure first for pending, newest first otherwise."""
    from radreport.db.models.knowledge import LexiconTerm

    query = select(LexiconSurfaceVariant, LexiconTerm.canonical_form, LexiconTerm.term_type).join(LexiconTerm, LexiconTerm.id == LexiconSurfaceVariant.lexicon_term_id).where(LexiconSurfaceVariant.tenant_id == tenant_id, LexiconSurfaceVariant.review_status == status)
    if status == "pending":
        return query.order_by(LexiconSurfaceVariant.observed_count.desc(), LexiconSurfaceVariant.confidence.desc(), LexiconSurfaceVariant.id)
    return query.order_by(LexiconSurfaceVariant.created_at.desc(), LexiconSurfaceVariant.id)


class ReviewRefused(ValueError):
    """No such variant in this lab, or not one a reviewer can decide."""


def record_review(session: Session, tenant_id: uuid.UUID, variant_id: uuid.UUID, answer: str, *, reviewer_id: uuid.UUID) -> LexiconSurfaceVariant:
    """same -> approved, different -> rejected, unsure -> stays pending. Reversing an automatic approval is logged as an override."""
    variant = session.get(LexiconSurfaceVariant, variant_id)
    if variant is None or variant.tenant_id != tenant_id:
        raise ReviewRefused("no such variant in this lab")
    if answer not in ("same", "different", "unsure"):
        raise ReviewRefused("answer must be same, different or unsure")
    previous = variant.review_status
    if answer == "unsure":
        session.add(AuditLog(tenant_id=tenant_id, actor_id=reviewer_id, actor_type=ActorType.USER, action="lexicon_variant_unsure", entity_type="lexicon_surface_variant", entity_id=variant.id, after={"surface": variant.surface_text}))
        session.flush()
        return variant
    variant.review_status = "approved" if answer == "same" else "rejected"
    variant.decided_by, variant.decided_at = reviewer_id, dt.datetime.now(dt.UTC)
    override = previous == "auto_approved" and variant.review_status == "rejected"
    session.add(AuditLog(tenant_id=tenant_id, actor_id=reviewer_id, actor_type=ActorType.USER, action="lexicon_variant_override" if override else "lexicon_variant_reviewed", entity_type="lexicon_surface_variant", entity_id=variant.id, before={"review_status": previous, "confidence": float(variant.confidence or 0)}, after={"review_status": variant.review_status, "arm": variant.threshold_arm}))
    session.flush()
    from radreport.cache import filters

    filters.forget_lexicon(tenant_id)
    return variant


def arm_stats(session: Session, tenant_id: uuid.UUID) -> dict[str, Any]:
    """Per arm: how many were auto-approved, how many of those a radiologist overrode, and how many waited."""
    overrides = select(AuditLog.entity_id).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "lexicon_variant_override")
    arm_of = func.coalesce(LexiconSurfaceVariant.threshold_arm, literal_column("'A'"))
    rows = session.execute(select(arm_of, func.count().filter(LexiconSurfaceVariant.review_status == "auto_approved"), func.count().filter(LexiconSurfaceVariant.review_status == "pending"), func.count().filter(LexiconSurfaceVariant.review_status == "approved"), func.count().filter(LexiconSurfaceVariant.review_status == "rejected"), func.sum(case((LexiconSurfaceVariant.id.in_(overrides), 1), else_=0))).where(LexiconSurfaceVariant.tenant_id == tenant_id, LexiconSurfaceVariant.confidence.isnot(None)).group_by(arm_of)).all()
    out = {}
    for arm, auto, waiting, approved, rejected, overridden in rows:
        auto_total = int(auto) + int(overridden or 0)
        out[arm] = {"auto_approved": int(auto), "pending": int(waiting), "approved": int(approved), "rejected": int(rejected), "overrides": int(overridden or 0), "override_rate": round(int(overridden or 0) / auto_total, 4) if auto_total else 0.0, "review_share": round(int(waiting) / max(1, int(auto) + int(waiting) + int(approved) + int(rejected)), 4)}
    return out
