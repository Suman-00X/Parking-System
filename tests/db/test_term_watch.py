"""New vocabulary from live edits: counted per lab, reviewed by a radiologist, approved into a new lexicon version."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from radreport.api.app import create_app
from radreport.db.models.knowledge import LexiconSet, LexiconTerm, PotentialLexiconTerm
from radreport.db.models.review import EditEvent, ReportRevision
from radreport.db.session import tenant_session
from radreport.knowledge.term_lookup import find_term
from radreport.onboarding import term_watch
from radreport.onboarding.lexicon import get_or_create_tenant_lexicon
from radreport.pipeline.stages.providers import load_tenant_knowledge
from tests.db.helpers import lab_headers
from tests.db.review_factory import build_signed_report

pytestmark = pytest.mark.db

EDITS = [("Lungs clear.", "Bilateral ground glass opacity with mosaic perfusion.")] * 3 + [("Normal.", "Ground glass opacity and crazy paving pattern.")] * 2 + [("Liver normal.", "Liver shows hepatic steatosis.")]


def _seed(db: str, lab) -> None:  # type: ignore[no-untyped-def]
    with tenant_session(lab, url=db) as session:
        build_signed_report(session, lab)
        revision = session.execute(select(ReportRevision.id).where(ReportRevision.tenant_id == lab)).scalars().first()
        lexicon = get_or_create_tenant_lexicon(session, lab)
        session.add(LexiconTerm(tenant_id=lab, lexicon_set_id=lexicon.id, canonical_form="fatty liver", term_type="pathology", phonetic_key_primary="FTLFR"))
        for n, (before, after) in enumerate(EDITS):
            session.add(EditEvent(tenant_id=lab, report_revision_id=revision, edit_type="value_change", before_value=before, after_value=after, created_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=len(EDITS) - n)))


def test_the_whole_workflow(migrated_db: str, two_tenants) -> None:
    lab, other = two_tenants
    _seed(migrated_db, lab)
    with tenant_session(lab, url=migrated_db) as session:
        first = term_watch.scan_edits(session, lab)
        again = term_watch.scan_edits(session, lab)
        waiting = {r.surface_text: r.frequency for r in session.execute(term_watch.pending(session, lab)).scalars()}
    assert first["edit_events"] == len(EDITS) and again["edit_events"] == 0, "the watermark stops a second count"
    assert waiting["ground glass opacity"] == 5 and waiting["mosaic perfusion"] == 3 and waiting["crazy paving pattern"] == 2
    assert "hepatic steatosis" not in waiting, "a synonym of a known term (fatty liver) is not new"
    assert "bilateral ground glass opacity" not in waiting and "liver shows" not in waiting, "filler-edged phrases are not offered"

    radiologist = lab_headers(migrated_db, lab, "radiologist")
    admin = lab_headers(migrated_db, lab, "lab_admin")
    client = TestClient(create_app())
    listing = client.get("/lexicon/candidates", headers=admin).json()
    by_term = {row["term"]: row["id"] for row in listing}
    assert client.post("/lexicon/candidates/approve", json={"ids": [by_term["ground glass opacity"]]}, headers=admin).status_code == 403, "only a radiologist approves"

    approved = client.post("/lexicon/candidates/approve", json={"ids": [by_term["ground glass opacity"], by_term["mosaic perfusion"]]}, headers=radiologist)
    assert approved.status_code == 200 and approved.json()["version"] == 2
    assert client.post("/lexicon/candidates/reject", json={"ids": [by_term["crazy paving pattern"]]}, headers=radiologist).json() == {"rejected": 1}
    assert client.post("/lexicon/candidates/approve", json={"ids": [by_term["mosaic perfusion"]]}, headers=radiologist).status_code == 409

    with tenant_session(lab, url=migrated_db) as session:
        versions = session.execute(select(LexiconSet.version, LexiconSet.is_active).where(LexiconSet.tenant_id == lab).order_by(LexiconSet.version)).all()
        assert [tuple(v) for v in versions] == [(1, False), (2, True)]
        knowledge = load_tenant_knowledge(session, lab)
        forms = [e.canonical_form for e in knowledge.lexicon]
        assert forms.count("fatty liver") == 1, "only the current version is loaded"
        assert {"ground glass opacity", "mosaic perfusion"} <= set(forms)
        assert find_term(session, lab, "Ground-glass opacity").how == "exact"
        statuses = dict(session.execute(select(PotentialLexiconTerm.surface_text, PotentialLexiconTerm.status).where(PotentialLexiconTerm.tenant_id == lab)).all())
        assert statuses["crazy paving pattern"] == "rejected" and statuses["ground glass opacity"] == "approved"
    with tenant_session(other, url=migrated_db) as session:
        assert session.execute(select(func.count()).select_from(PotentialLexiconTerm)).scalar_one() == 0


def test_the_review_page(migrated_db: str, two_tenants) -> None:
    lab, _ = two_tenants
    _seed(migrated_db, lab)
    with tenant_session(lab, url=migrated_db) as session:
        term_watch.scan_edits(session, lab)
    client = TestClient(create_app())
    page = client.get("/ui/lexicon", headers=lab_headers(migrated_db, lab, "radiologist"))
    assert page.status_code == 200 and "ground glass opacity" in page.text and "Approve selected" in page.text
    assert 'href="/ui/lexicon"' in client.get("/ui/queue", headers=lab_headers(migrated_db, lab, "radiologist")).text
