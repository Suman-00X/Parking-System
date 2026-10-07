"""Per-lab Bloom filters that let common lookups skip the database when the answer is a definite "no".

Order: build a lab's filter from the database the first time it is needed, and again after it ages
(_TenantFilters.get) -> recordings: is this audio hash maybe already stored (maybe_seen_recording,
remember_recording) -> lexicon: is this term maybe one the lab knows (maybe_known_term,
forget_lexicon). A "maybe" always goes on to the database; only a "no" saves work. Each worker
holds its own filters, so a "no" can miss a row another worker just wrote: callers keep the
database constraint as the final word.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from radreport.db.models.ingestion import Recording
from radreport.db.models.knowledge import USED_VARIANTS, LexiconSet, LexiconSurfaceVariant, LexiconTerm
from radreport.knowledge.bloom import BloomFilter
from radreport.knowledge.lexicon_versions import current_set

#: How long a lab's filter is trusted before it is rebuilt from the database.
MAX_AGE_SECONDS = 600.0


class _TenantFilters:
    def __init__(self, loader: Callable[[Session, uuid.UUID], list[str]], *, fp_rate: float) -> None:
        self._loader = loader
        self._fp_rate = fp_rate
        self._filters: dict[uuid.UUID, tuple[float, BloomFilter]] = {}
        self._lock = threading.Lock()

    def get(self, session: Session, tenant_id: uuid.UUID) -> BloomFilter:
        with self._lock:
            entry = self._filters.get(tenant_id)
        if entry is not None and time.monotonic() - entry[0] < MAX_AGE_SECONDS:
            return entry[1]
        bloom = BloomFilter.of(self._loader(session, tenant_id), fp_rate=self._fp_rate)
        with self._lock:
            self._filters[tenant_id] = (time.monotonic(), bloom)
        return bloom

    def add(self, tenant_id: uuid.UUID, item: str) -> None:
        with self._lock:
            entry = self._filters.get(tenant_id)
        if entry is not None:
            entry[1].add(item)

    def drop(self, tenant_id: uuid.UUID | None = None) -> None:
        with self._lock:
            if tenant_id is None:
                self._filters.clear()
            else:
                self._filters.pop(tenant_id, None)


def _recording_hashes(session: Session, tenant_id: uuid.UUID) -> list[str]:
    return list(session.execute(select(Recording.content_hash).where(Recording.tenant_id == tenant_id)).scalars().all())


def _known_terms(session: Session, tenant_id: uuid.UUID) -> list[str]:
    forms = session.execute(select(LexiconTerm.canonical_form).join(LexiconSet, LexiconSet.id == LexiconTerm.lexicon_set_id).where((LexiconSet.tenant_id == tenant_id) | LexiconSet.tenant_id.is_(None), current_set())).scalars().all()
    shorts = session.execute(select(LexiconTerm.short_form).join(LexiconSet, LexiconSet.id == LexiconTerm.lexicon_set_id).where(LexiconSet.tenant_id == tenant_id, LexiconTerm.short_form.isnot(None), current_set())).scalars().all()
    variants = session.execute(select(LexiconSurfaceVariant.surface_text).where(LexiconSurfaceVariant.tenant_id == tenant_id, LexiconSurfaceVariant.review_status.in_(USED_VARIANTS))).scalars().all()
    from radreport.knowledge.synonyms import normalise

    # The same normalised form the lexicon lookup compares, so the filter never calls "ground-glass" new when "ground glass" is known.
    return [normalise(t) for t in (*forms, *shorts, *variants) if t]


RECORDINGS = _TenantFilters(_recording_hashes, fp_rate=0.001)
TERMS = _TenantFilters(_known_terms, fp_rate=0.01)


def maybe_seen_recording(session: Session, tenant_id: uuid.UUID, content_hash: str) -> bool:
    return content_hash in RECORDINGS.get(session, tenant_id)


def remember_recording(tenant_id: uuid.UUID, content_hash: str) -> None:
    RECORDINGS.add(tenant_id, content_hash)


def maybe_known_term(session: Session, tenant_id: uuid.UUID, term: str) -> bool:
    from radreport.knowledge.synonyms import normalise

    return normalise(term) in TERMS.get(session, tenant_id)


def forget_lexicon(tenant_id: uuid.UUID) -> None:
    """Rebuild the lab's term filter on next use, after its lexicon changed."""
    TERMS.drop(tenant_id)
