"""Finds which of a lab's lexicon terms a piece of text refers to: by its exact wording, a short form, a heard variant, a synonym, or its English when written in another language.

Order: build the lab's lookup once per request (lab_terms) -> resolve a phrase (find_term), trying
the exact forms first, the curated synonym set second, and the English for a term in one of the lab's
other languages last -> say whether a phrase is new to the lab
(is_unknown), which the Bloom filter answers first when it can say "definitely not".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.cache import request
from radreport.cache.filters import maybe_known_term
from radreport.cache.keys import key
from radreport.db.models.knowledge import USED_VARIANTS, LexiconSet, LexiconSurfaceVariant, LexiconTerm
from radreport.knowledge import languages
from radreport.knowledge.lexicon_versions import current_set
from radreport.knowledge.synonyms import concept_of, normalise


@dataclass(frozen=True, slots=True)
class TermMatch:
    term_id: uuid.UUID
    canonical_form: str
    how: str
    """exact | short_form | variant | synonym | translated_<language>"""

    confidence: float


@dataclass(frozen=True, slots=True)
class LabTerms:
    by_form: dict[str, tuple[uuid.UUID, str, str]]
    by_concept: dict[str, tuple[uuid.UUID, str]]


def lab_terms(session: Session, tenant_id: uuid.UUID) -> LabTerms:
    """Every way the lab writes or says its terms, read once per request."""

    def load() -> LabTerms:
        by_form: dict[str, tuple[uuid.UUID, str, str]] = {}
        by_concept: dict[str, tuple[uuid.UUID, str]] = {}
        rows = session.execute(select(LexiconTerm.id, LexiconTerm.canonical_form, LexiconTerm.short_form).join(LexiconSet, LexiconSet.id == LexiconTerm.lexicon_set_id).where((LexiconSet.tenant_id == tenant_id) | LexiconSet.tenant_id.is_(None), current_set())).all()
        for term_id, form, short in rows:
            by_form.setdefault(normalise(form), (term_id, form, "exact"))
            if short:
                by_form.setdefault(normalise(short), (term_id, form, "short_form"))
            concept = concept_of(form)
            if concept:
                by_concept.setdefault(concept, (term_id, form))
        names = {term_id: form for term_id, form, _ in rows}
        for term_id, surface in session.execute(select(LexiconSurfaceVariant.lexicon_term_id, LexiconSurfaceVariant.surface_text).where(LexiconSurfaceVariant.tenant_id == tenant_id, LexiconSurfaceVariant.review_status.in_(USED_VARIANTS))).all():
            if term_id in names:
                by_form.setdefault(normalise(surface), (term_id, names[term_id], "variant"))
        return LabTerms(by_form=by_form, by_concept=by_concept)

    return request.request_cached(key("lab_terms", tenant_id), load)


def find_term(session: Session, tenant_id: uuid.UUID, text: str, *, translate: bool = True) -> TermMatch | None:
    terms = lab_terms(session, tenant_id)
    form = normalise(text)
    hit = terms.by_form.get(form)
    if hit is not None:
        return TermMatch(term_id=hit[0], canonical_form=hit[1], how=hit[2], confidence=1.0)
    concept = concept_of(form)
    if concept is not None and concept in terms.by_concept:
        term_id, canonical = terms.by_concept[concept]
        return TermMatch(term_id=term_id, canonical_form=canonical, how="synonym", confidence=0.95)
    english = _in_english(session, tenant_id, text) if translate else None
    if english is not None:
        inner = find_term(session, tenant_id, english[0], translate=False)
        if inner is not None:
            return TermMatch(term_id=inner.term_id, canonical_form=inner.canonical_form, how=f"translated_{english[1]}", confidence=round(0.9 * inner.confidence, 4))
    return None


def _in_english(session: Session, tenant_id: uuid.UUID, text: str) -> tuple[str, str] | None:
    """(English term, language) when the whole phrase is one term in a language the lab has on."""
    lab = request.request_cached(key("lab_languages", tenant_id), lambda: languages.enabled_languages(session, tenant_id))
    if not lab.any:
        return None
    spans = languages.find_foreign(text, languages.glossary(lab.languages, lab.latin_hindi))
    if len(spans) == 1 and spans[0].start == 0 and spans[0].end >= len(text.rstrip(" .,;:!?।")):
        return spans[0].english, spans[0].language
    return None


def is_unknown(session: Session, tenant_id: uuid.UUID, text: str) -> bool:
    """True when nothing in the lab's lexicon, or a synonym of it, covers the phrase."""
    if not maybe_known_term(session, tenant_id, text) and concept_of(text) is None and _in_english(session, tenant_id, text) is None:
        return True  # the filter is certain the exact wording is new, and it has no synonym to check
    return find_term(session, tenant_id, text) is None
