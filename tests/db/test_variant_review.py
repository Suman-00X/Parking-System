"""Sound-alike matches: confident ones are used, unsure ones are asked about, and overrides are counted per threshold arm."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from radreport.api.app import create_app
from radreport.core import system_config
from radreport.db.models.knowledge import LexiconSurfaceVariant, LexiconTerm
from radreport.db.models.orchestration import AuditLog
from radreport.db.session import tenant_session
from radreport.knowledge import variant_review
from radreport.onboarding.lexicon import get_or_create_tenant_lexicon
from radreport.pipeline.stages.providers import load_tenant_knowledge
from tests.db.helpers import lab_headers

pytestmark = pytest.mark.db


def _seed(db: str, lab) -> dict[str, str]:  # type: ignore[no-untyped-def]
    with tenant_session(lab, url=db) as session:
        lexicon = get_or_create_tenant_lexicon(session, lab)
        liver = LexiconTerm(tenant_id=lab, lexicon_set_id=lexicon.id, canonical_form="pleural effusion", term_type="pathology", phonetic_key_primary="PLRLFJN")
        session.add(liver)
        session.flush()
        rows = {"pleural fusion": ("auto_approved", 0.93), "plural effusion": ("pending", 0.8), "peril fusion": ("pending", 0.62), "pleura fusing": ("rejected", 0.7)}
        made = {}
        for surface, (status, confidence) in rows.items():
            variant = LexiconSurfaceVariant(tenant_id=lab, lexicon_term_id=liver.id, surface_text=surface, phonetic_key="PLRL", observed_count=3, source="mined", review_status=status, confidence=confidence, threshold_arm="A")
            session.add(variant)
            session.flush()
            made[surface] = str(variant.id)
        return made


def test_only_used_variants_reach_the_pipeline(migrated_db: str, two_tenants) -> None:
    lab, _ = two_tenants
    _seed(migrated_db, lab)
    with tenant_session(lab, url=migrated_db) as session:
        entry = next(e for e in load_tenant_knowledge(session, lab).lexicon if e.canonical_form == "pleural effusion")
    assert entry.surface_variants == ("pleural fusion",), "pending and rejected variants never correct a transcript"


def test_review_queue_answers_and_override_stats(migrated_db: str, two_tenants) -> None:
    lab, other = two_tenants
    ids = _seed(migrated_db, lab)
    client = TestClient(create_app())
    radiologist, admin = lab_headers(migrated_db, lab, "radiologist"), lab_headers(migrated_db, lab, "lab_admin")

    waiting = client.get("/lexicon/variants", headers=admin)
    assert waiting.status_code == 200 and waiting.headers["x-total-count"] == "2"
    assert [r["heard"] for r in waiting.json()][0] in {"plural effusion", "peril fusion"} and all(r["term"] == "pleural effusion" for r in waiting.json())
    assert [r["heard"] for r in client.get("/lexicon/variants?review_status=auto_approved", headers=admin).json()] == ["pleural fusion"]
    assert client.post(f"/lexicon/variants/{ids['plural effusion']}/decide", json={"answer": "same"}, headers=admin).status_code == 403, "only a radiologist answers"

    assert client.post(f"/lexicon/variants/{ids['plural effusion']}/decide", json={"answer": "same"}, headers=radiologist).json()["review_status"] == "approved"
    assert client.post(f"/lexicon/variants/{ids['peril fusion']}/decide", json={"answer": "unsure"}, headers=radiologist).json()["review_status"] == "pending"
    assert client.post(f"/lexicon/variants/{ids['pleural fusion']}/decide", json={"answer": "different"}, headers=radiologist).json()["review_status"] == "rejected"
    assert client.post(f"/lexicon/variants/{ids['pleural fusion']}/decide", json={"answer": "maybe"}, headers=radiologist).status_code in (400, 422)
    other_headers = lab_headers(migrated_db, other, "radiologist")
    assert client.post(f"/lexicon/variants/{ids['peril fusion']}/decide", json={"answer": "same"}, headers=other_headers).status_code == 404, "another lab cannot answer for this one"

    stats = client.get("/lexicon/variants/stats", headers=admin).json()
    assert stats["arm"] == "A" and stats["arms"]["A"]["overrides"] == 1 and stats["arms"]["A"]["override_rate"] == 1.0
    with tenant_session(lab, url=migrated_db) as session:
        actions = set(session.execute(select(AuditLog.action).where(AuditLog.tenant_id == lab, AuditLog.action.like("lexicon_variant%"))).scalars())
        assert {"lexicon_variant_override", "lexicon_variant_reviewed", "lexicon_variant_unsure"} <= actions
        entry = next(e for e in load_tenant_knowledge(session, lab).lexicon if e.canonical_form == "pleural effusion")
        assert entry.surface_variants == ("plural effusion",)

    page = client.get("/ui/lexicon", headers=radiologist)
    assert page.status_code == 200 and "Is this the same term?" in page.text and "peril fusion" in page.text and "Yes, same" in page.text


def test_thresholds_per_type_and_arm(migrated_db: str, two_tenants) -> None:
    lab, _ = two_tenants
    with tenant_session(lab, url=migrated_db) as session:
        general = variant_review.thresholds(session, lab, "pathology")
        strict = variant_review.thresholds(session, lab, "abbreviation")
        assert (general.auto_approve_above, general.review_above, general.arm) == (0.85, 0.6, "A") and strict.auto_approve_above == 0.92
        system_config.set_value(session, "lexicon.ab_test", 1, actor_id=uuid.uuid4(), tenant_id=lab)
        arm = variant_review.threshold_arm(session, lab)
        loosened = variant_review.thresholds(session, lab, "pathology")
        assert loosened.arm == arm and loosened.auto_approve_above == (0.80 if arm == "B" else 0.85)
        assert variant_review.thresholds(session, lab, "abbreviation").auto_approve_above == 0.92, "arm B never loosens abbreviations"
