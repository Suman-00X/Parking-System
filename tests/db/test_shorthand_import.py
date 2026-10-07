"""Shorthand references through the admin panel into the lab's lexicon, its audit trail, and the speech engine's biasing list."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from radreport.db.models.knowledge import LexiconTerm
from radreport.db.models.orchestration import AuditLog
from radreport.db.session import tenant_session
from radreport.pipeline.stages.asr import build_keyterms
from radreport.pipeline.stages.providers import load_tenant_knowledge
from tests.db.helpers import make_platform_user, signed_in
from tests.fixtures.pdf import text_pdf

pytestmark = pytest.mark.db


def test_a_pdf_reference_becomes_lexicon_terms_with_an_audit_trail(migrated_db: str, two_tenants) -> None:
    lab, other = two_tenants
    client = signed_in(make_platform_user(migrated_db))
    pdf = text_pdf(["Shorthand used in this department", "LLL = Left lower lobe", "GGO -> ground glass opacity", "PNA | Pneumonia"])
    response = client.post(f"/admin/api/labs/{lab}/onboarding/shorthand", files=[("files", ("reference.pdf", pdf, "application/pdf"))])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["mappings"] == 3 and body["terms_created"] == 3

    with tenant_session(lab, url=migrated_db) as session:
        terms = {t.short_form: t.canonical_form for t in session.execute(select(LexiconTerm).where(LexiconTerm.tenant_id == lab, LexiconTerm.short_form.isnot(None))).scalars()}
        assert terms == {"LLL": "Left lower lobe", "GGO": "ground glass opacity", "PNA": "Pneumonia"}
        trail = session.execute(select(AuditLog.after).where(AuditLog.tenant_id == lab, AuditLog.action == "shorthand_mapped")).scalars().all()
        assert {(a["short"], a["filename"], a["line"]) for a in trail} == {("LLL", "reference.pdf", 2), ("GGO", "reference.pdf", 3), ("PNA", "reference.pdf", 4)}
        keyterms = build_keyterms(load_tenant_knowledge(session, lab))
    assert {"LLL", "Left lower lobe", "GGO", "PNA"} <= set(keyterms), "short forms bias the speech engine"
    with tenant_session(other, url=migrated_db) as session:
        assert not session.execute(select(LexiconTerm).where(LexiconTerm.short_form == "LLL")).scalars().all(), "another lab's lexicon is untouched"


def test_a_short_form_with_two_meanings_is_flagged_not_overwritten(migrated_db: str, two_tenants) -> None:
    lab, _ = two_tenants
    client = signed_in(make_platform_user(migrated_db))
    client.post(f"/admin/api/labs/{lab}/onboarding/shorthand", files=[("files", ("a.txt", b"PE = Pulmonary embolism\n", "text/plain"))])
    second = client.post(f"/admin/api/labs/{lab}/onboarding/shorthand", files=[("files", ("b.txt", b"PE = Pleural effusion\n", "text/plain"))]).json()
    assert second["conflicts"] and "Pulmonary embolism" in second["conflicts"][0]
    with tenant_session(lab, url=migrated_db) as session:
        effusion = session.execute(select(LexiconTerm).where(LexiconTerm.tenant_id == lab, LexiconTerm.canonical_form == "Pleural effusion")).scalar_one()
        assert effusion.short_form is None and effusion.is_ambiguous


def test_the_onboarding_page_offers_the_upload(migrated_db: str, two_tenants) -> None:
    lab, _ = two_tenants
    client = signed_in(make_platform_user(migrated_db))
    assert f"/admin/labs/{lab}/onboarding/shorthand" in client.get(f"/admin/labs/{lab}/onboarding").text
    done = client.post(f"/admin/labs/{lab}/onboarding/shorthand", files=[("files", ("s.txt", b"CBD: common bile duct\n", "text/plain"))])
    assert done.status_code == 303 and "Shorthand+added" in done.headers["location"]
