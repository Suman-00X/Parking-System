"""Watches what radiologists type into reports for vocabulary the lab's lexicon lacks, and turns their approvals into a new lexicon version.

Order: take the words an edit added (added_text) -> cut them into candidate terms (candidates: two-
to four-word phrases, acronyms, long single words, never filler) -> keep those the lexicon and its
synonyms do not already cover (scan_edits, from the lab's edit events since the last scan, with a
per-lab watermark) -> list them for review, most frequent first (pending) -> on a radiologist's
approval, make the next lexicon version with the new terms in it (approve), or set them aside (reject).
The watch_lexicon job runs the scan for every lab once a day.
"""

from __future__ import annotations

import datetime as dt
import difflib
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from radreport.cache import filters, request
from radreport.cache.keys import key
from radreport.core.logging import get_logger
from radreport.core.types import ActorType, TermType
from radreport.db.models.knowledge import LexiconSet, LexiconWatchState, PotentialLexiconTerm
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.review import EditEvent
from radreport.knowledge.lexicon_versions import new_version
from radreport.knowledge.phonetics import double_metaphone
from radreport.knowledge.synonyms import normalise
from radreport.knowledge.term_lookup import is_unknown
from radreport.onboarding.lexicon import get_or_create_tenant_lexicon

log = get_logger(__name__)

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*|\d+(?:\.\d+)?")
_ACRONYM = re.compile(r"^[A-Z]{2,6}$")
#: Words that never start or end a term: grammar, hedging, and the scaffolding of a report sentence.
STOPWORDS = frozenset(
    """a an the is are was were be been being of in on at to for from by with without within and or nor but not no there here this that these those it its
    seen noted shows show showing appears appear appearing likely unlikely possible possibly probable probably suggest suggests suggestive suggesting
    evidence of finding findings impression study examination exam note noted measuring measures measure measured approximately about around
    normal abnormal mild mildly moderate moderately severe severely small large minimal minor significant significantly slight slightly
    left right bilateral both upper lower mid middle anterior posterior lateral medial superior inferior
    size sized shape outline contour echotexture density attenuation signal intensity
    is as also again still now new old previous prior compared comparison since than
    cm mm ml cc hu x""".split()
)
#: A single word must be at least this long to count on its own; shorter ones are almost always ordinary English.
MIN_SINGLE_WORD = 8
MAX_CONTEXTS = 3
SCAN_BATCH = 5000


@dataclass(frozen=True, slots=True)
class Candidate:
    surface: str
    normalized: str
    term_type: str


def added_text(before: str | None, after: str | None) -> list[list[str]]:
    """The runs of words the edit put in that were not there before."""
    old, new = _WORD.findall(before or ""), _WORD.findall(after or "")
    runs: list[list[str]] = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(a=[w.lower() for w in old], b=[w.lower() for w in new], autojunk=False).get_opcodes():
        if tag in ("insert", "replace") and j2 > j1:
            runs.append(new[j1:j2])
    return runs


def candidates(words: list[str]) -> list[Candidate]:
    """Terms worth a radiologist's look in one run of words."""
    out: dict[str, Candidate] = {}
    lowered = [w.lower() for w in words]
    for i, word in enumerate(words):
        if _ACRONYM.match(word):
            out.setdefault(word.lower(), Candidate(surface=word, normalized=normalise(word), term_type=TermType.ABBREVIATION))
        elif len(word) >= MIN_SINGLE_WORD and lowered[i] not in STOPWORDS and word.isalpha():
            out.setdefault(lowered[i], Candidate(surface=lowered[i], normalized=normalise(word), term_type=TermType.PATHOLOGY))
        for size in (2, 3, 4):
            gram = lowered[i : i + size]
            if len(gram) < size or gram[0] in STOPWORDS or gram[-1] in STOPWORDS or any(not g.isalpha() for g in gram):
                continue
            if sum(1 for g in gram if g not in STOPWORDS) < 2:
                continue
            phrase = " ".join(gram)
            out.setdefault(phrase, Candidate(surface=phrase, normalized=normalise(phrase), term_type=TermType.PATHOLOGY))
    return list(out.values())


def _sentence_around(text: str, phrase: str) -> str:
    for sentence in re.split(r"(?<=[.;])\s+", text or ""):
        if phrase.lower() in sentence.lower():
            return sentence.strip()[:240]
    return (text or "")[:240]


def scan_edits(session: Session, tenant_id: uuid.UUID, *, now: dt.datetime | None = None) -> dict[str, int]:
    """Read the lab's edit events since the last scan and count the unknown terms in what was added."""
    moment = now or dt.datetime.now(dt.UTC)
    state = session.execute(select(LexiconWatchState).where(LexiconWatchState.tenant_id == tenant_id)).scalar_one_or_none()
    if state is None:
        state = LexiconWatchState(tenant_id=tenant_id)
        session.add(state)
        session.flush()
    query = select(EditEvent.created_at, EditEvent.before_value, EditEvent.after_value).where(EditEvent.tenant_id == tenant_id).order_by(EditEvent.created_at).limit(SCAN_BATCH)
    if state.last_edit_event_at is not None:
        query = query.where(EditEvent.created_at > state.last_edit_event_at)
    events = session.execute(query).all()

    counts: Counter[str] = Counter()
    first: dict[str, Candidate] = {}
    contexts: dict[str, list[str]] = {}
    unknown_cache: dict[str, bool] = {}
    for _created, before, after in events:
        for run in added_text(before, after):
            for candidate in candidates(run):
                known = unknown_cache.get(candidate.normalized)
                if known is None:
                    known = unknown_cache[candidate.normalized] = is_unknown(session, tenant_id, candidate.surface)
                if not known:
                    continue
                counts[candidate.normalized] += 1
                first.setdefault(candidate.normalized, candidate)
                bucket = contexts.setdefault(candidate.normalized, [])
                if len(bucket) < MAX_CONTEXTS:
                    bucket.append(_sentence_around(after or "", candidate.surface))

    for normalized, frequency in counts.items():
        candidate = first[normalized]
        statement = insert(PotentialLexiconTerm).values(tenant_id=tenant_id, surface_text=candidate.surface, normalized_text=normalized, term_type=candidate.term_type, frequency=frequency, contexts=contexts[normalized], first_seen_at=moment, last_seen_at=moment)
        session.execute(statement.on_conflict_do_update(index_elements=["tenant_id", "normalized_text"], set_={"frequency": PotentialLexiconTerm.frequency + statement.excluded.frequency, "last_seen_at": statement.excluded.last_seen_at}))
    if events:
        state.last_edit_event_at = events[-1][0]
    state.last_run_at = moment
    session.flush()
    log.info("lexicon_watch_scanned", tenant_id=str(tenant_id), edit_events=len(events), unknown_terms=len(counts))
    return {"edit_events": len(events), "unknown_terms": len(counts), "occurrences": sum(counts.values())}


def pending(session: Session, tenant_id: uuid.UUID, *, min_frequency: int = 1) -> Any:
    """The review queue query: pending terms, most used first."""
    return select(PotentialLexiconTerm).where(PotentialLexiconTerm.tenant_id == tenant_id, PotentialLexiconTerm.status == "pending", PotentialLexiconTerm.frequency >= min_frequency).order_by(PotentialLexiconTerm.frequency.desc(), PotentialLexiconTerm.last_seen_at.desc(), PotentialLexiconTerm.id)


class DecisionRefused(ValueError):
    """Nothing pending matched the ids given."""


def approve(session: Session, tenant_id: uuid.UUID, ids: list[uuid.UUID], *, approver_id: uuid.UUID) -> LexiconSet:
    """Add the terms to the lab's lexicon as its next version, and record who approved them."""
    rows = list(session.execute(select(PotentialLexiconTerm).where(PotentialLexiconTerm.tenant_id == tenant_id, PotentialLexiconTerm.id.in_(ids), PotentialLexiconTerm.status == "pending")).scalars())
    if not rows:
        raise DecisionRefused("none of those terms is waiting for review")
    current = get_or_create_tenant_lexicon(session, tenant_id)
    additions = []
    for row in rows:
        primary, secondary = double_metaphone(row.surface_text)
        additions.append({"canonical_form": row.surface_text, "term_type": row.term_type, "phonetic_key_primary": primary, "phonetic_key_secondary": secondary, "frequency_rank": row.frequency})
    successor = new_version(session, current, add_terms=additions)
    now = dt.datetime.now(dt.UTC)
    session.execute(update(PotentialLexiconTerm).where(PotentialLexiconTerm.id.in_([r.id for r in rows])).values(status="approved", approved=True, approved_at=now, decided_by=approver_id, lexicon_set_version_id=successor.id))
    session.add(AuditLog(tenant_id=tenant_id, actor_id=approver_id, actor_type=ActorType.USER, action="lexicon_terms_approved", entity_type="lexicon_set", entity_id=successor.id, before={"version": current.version}, after={"version": successor.version, "terms": [r.surface_text for r in rows]}))
    session.flush()
    _changed(tenant_id)
    log.info("lexicon_terms_approved", tenant_id=str(tenant_id), terms=len(rows), version=successor.version)
    return successor


def reject(session: Session, tenant_id: uuid.UUID, ids: list[uuid.UUID], *, reviewer_id: uuid.UUID) -> int:
    """Set terms aside; they keep counting but are not offered again."""
    result = session.execute(update(PotentialLexiconTerm).where(PotentialLexiconTerm.tenant_id == tenant_id, PotentialLexiconTerm.id.in_(ids), PotentialLexiconTerm.status == "pending").values(status="rejected", decided_by=reviewer_id))
    if not result.rowcount:  # type: ignore[attr-defined]
        raise DecisionRefused("none of those terms is waiting for review")
    session.add(AuditLog(tenant_id=tenant_id, actor_id=reviewer_id, actor_type=ActorType.USER, action="lexicon_terms_rejected", entity_type="potential_lexicon_term", entity_id=None, after={"ids": [str(i) for i in ids]}))
    session.flush()
    return int(result.rowcount)  # type: ignore[attr-defined]


def _changed(tenant_id: uuid.UUID) -> None:
    filters.forget_lexicon(tenant_id)
    request.forget(key("lab_terms", tenant_id))
