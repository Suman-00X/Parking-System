"""Lexicon sets are versioned: a change makes a new version and the old one stays as history. This module says which version is current and makes the next one.

Order: the clause every reader filters on (current_set: a set with no newer version of the same name)
-> copy the current set with its terms and heard variants into the next version, plus new terms
(new_version, in batched inserts).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.orm import Session, aliased

from radreport.db.bulk import bulk_insert
from radreport.db.models.knowledge import LexiconSet, LexiconSurfaceVariant, LexiconTerm

_TERM_COPY = ("canonical_form", "short_form", "term_type", "phonetic_key_primary", "phonetic_key_secondary", "frequency_rank", "radlex_id", "snomed_ct_id", "is_ambiguous", "expansion_policy")
_VARIANT_COPY = ("surface_text", "phonetic_key", "observed_count", "source", "confidence", "review_status", "threshold_arm", "decided_by", "decided_at")


def current_set() -> ColumnElement[bool]:
    """True for a lexicon set that no newer version of the same name and scope has replaced."""
    newer = aliased(LexiconSet)
    same_owner = or_(newer.tenant_id == LexiconSet.tenant_id, and_(newer.tenant_id.is_(None), LexiconSet.tenant_id.is_(None)))
    return ~exists().where(newer.name == LexiconSet.name, newer.scope == LexiconSet.scope, newer.version > LexiconSet.version, same_owner)


def new_version(session: Session, current: LexiconSet, *, add_terms: Iterable[dict[str, Any]] = ()) -> LexiconSet:
    """The next version of `current`: every term and variant copied, then `add_terms` (column dicts) added. The old version is kept and deactivated."""
    successor = LexiconSet(id=uuid.uuid4(), tenant_id=current.tenant_id, name=current.name, version=current.version + 1, scope=current.scope, scope_ref=current.scope_ref, is_active=True)
    session.add(successor)
    current.is_active = False
    session.flush()
    terms = list(session.execute(select(LexiconTerm).where(LexiconTerm.lexicon_set_id == current.id)).scalars())
    mapping = {t.id: uuid.uuid4() for t in terms}
    bulk_insert(session, LexiconTerm, [{"id": mapping[t.id], "tenant_id": t.tenant_id, "lexicon_set_id": successor.id, **{c: getattr(t, c) for c in _TERM_COPY}} for t in terms])
    variants = list(session.execute(select(LexiconSurfaceVariant).where(LexiconSurfaceVariant.lexicon_term_id.in_(list(mapping)))).scalars()) if mapping else []
    bulk_insert(session, LexiconSurfaceVariant, [{"tenant_id": v.tenant_id, "lexicon_term_id": mapping[v.lexicon_term_id], **{c: getattr(v, c) for c in _VARIANT_COPY}} for v in variants])
    existing = {t.canonical_form.lower() for t in terms}
    bulk_insert(session, LexiconTerm, [{"tenant_id": current.tenant_id, "lexicon_set_id": successor.id, **row} for row in add_terms if row["canonical_form"].lower() not in existing])
    session.flush()
    return successor
