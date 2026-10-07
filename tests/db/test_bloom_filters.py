"""The per-lab filters in front of the database: a definite "no" skips the lookup, and a stale filter cannot let a duplicate in."""

from __future__ import annotations

import uuid

import pytest

from radreport.adapters.storage.object_store import InMemoryObjectStore
from radreport.cache import filters
from radreport.core.errors import DuplicateRecording
from radreport.db import instrumentation
from radreport.db.session import tenant_session
from radreport.devtools.synthetic import synth_audio
from radreport.ingest.service import IngestRequest, ingest_recording
from tests.db.test_job_queue import _seed

pytestmark = pytest.mark.db


def _request(lab: dict, data: bytes) -> IngestRequest:
    return IngestRequest(tenant_id=uuid.UUID(lab["tenant_id"]), study_id=uuid.UUID(lab["study_id"]), radiologist_id=uuid.UUID(lab["radiologist_id"]), filename="d.flac", data=data)


def test_a_new_recording_skips_the_duplicate_lookup(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    lab = _seed(migrated_db, tenant_id)
    filters.RECORDINGS.drop()
    with tenant_session(tenant_id, url=migrated_db) as session:
        filters.RECORDINGS.get(session, tenant_id)  # built once, as on a warm worker
        with instrumentation.query_scope("ingest", keep_statements=True) as stats:
            ingest_recording(session, InMemoryObjectStore(), _request(lab, synth_audio(seconds=8, seed=3)))
    assert not [s for s in stats.statements or [] if s.startswith("SELECT recording.")], "no lookup for audio the filter has never seen"


def test_a_repeat_upload_is_still_a_duplicate(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    lab = _seed(migrated_db, tenant_id)
    data = synth_audio(seconds=8, seed=4)
    with tenant_session(tenant_id, url=migrated_db) as session:
        ingest_recording(session, InMemoryObjectStore(), _request(lab, data))
    with pytest.raises(DuplicateRecording), tenant_session(tenant_id, url=migrated_db) as session:
        ingest_recording(session, InMemoryObjectStore(), _request(lab, data))


def test_a_stale_filter_falls_back_to_the_constraint(migrated_db: str, two_tenants) -> None:
    """Another worker stored this audio after our filter was built: the unique constraint catches it and it is reported as a duplicate."""
    tenant_id, _ = two_tenants
    lab = _seed(migrated_db, tenant_id)
    data = synth_audio(seconds=8, seed=5)
    filters.RECORDINGS.drop()
    with tenant_session(tenant_id, url=migrated_db) as session:
        filters.RECORDINGS.get(session, tenant_id)  # our worker's filter, built before the other upload
    with tenant_session(tenant_id, url=migrated_db) as session:
        ingest_recording(session, InMemoryObjectStore(), _request(lab, data))
    filters.RECORDINGS._filters[tenant_id][1].__init__(bits=1024, hashes=3)  # noqa: SLF001 - simulate a filter that never saw that upload
    with pytest.raises(DuplicateRecording), tenant_session(tenant_id, url=migrated_db) as session:
        ingest_recording(session, InMemoryObjectStore(), _request(lab, data))


def test_known_terms_include_the_labs_lexicon(migrated_db: str, two_tenants) -> None:
    from radreport.db.models.knowledge import LexiconTerm
    from radreport.onboarding.lexicon import get_or_create_tenant_lexicon

    tenant_id, other = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        lexicon = get_or_create_tenant_lexicon(session, tenant_id)
        session.add(LexiconTerm(tenant_id=tenant_id, lexicon_set_id=lexicon.id, canonical_form="ground glass opacity", term_type="pathology", phonetic_key_primary="KRNT"))
        session.flush()
        filters.forget_lexicon(tenant_id)
        assert filters.maybe_known_term(session, tenant_id, "Ground Glass Opacity")
        assert not filters.maybe_known_term(session, tenant_id, "mosaic perfusion")
    with tenant_session(other, url=migrated_db) as session:
        filters.forget_lexicon(other)
        assert not filters.maybe_known_term(session, other, "ground glass opacity"), "one lab's terms never reach another's filter"
