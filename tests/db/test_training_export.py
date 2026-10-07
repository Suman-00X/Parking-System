"""Approvals export as training data only from labs that agreed, with no report text, and with stable train/eval halves."""

from __future__ import annotations

import json
import uuid

import pytest

from radreport.core import system_config
from radreport.db.models.knowledge import LexiconSurfaceVariant, LexiconTerm, PotentialLexiconTerm
from radreport.db.session import tenant_session
from radreport.devtools.template_eval import FIXTURES
from radreport.devtools.training_export import lab_sessions
from radreport.knowledge import training_data
from radreport.onboarding import templates
from radreport.onboarding.batches import ArtifactUpload
from radreport.onboarding.lexicon import get_or_create_tenant_lexicon

pytestmark = pytest.mark.db


def _seed(db: str, lab: uuid.UUID, *, share: bool) -> None:
    with tenant_session(lab, url=db) as session:
        submission = templates.submit_templates(session, tenant_id=lab, uploads=[ArtifactUpload("structured_ct_chest.txt", (FIXTURES / "structured_ct_chest.txt").read_bytes())])
        submission.candidates[0].review_status = "approved"
        lexicon = get_or_create_tenant_lexicon(session, lab)
        term = LexiconTerm(tenant_id=lab, lexicon_set_id=lexicon.id, canonical_form="pleural effusion", term_type="pathology", phonetic_key_primary="X")
        session.add(term)
        session.flush()
        reviewer = uuid.uuid4()
        session.add_all([LexiconSurfaceVariant(tenant_id=lab, lexicon_term_id=term.id, surface_text=s, phonetic_key="X", observed_count=1, source="mined", review_status=st, decided_by=reviewer if decided else None, confidence=0.7) for s, st, decided in (("plural effusion", "approved", True), ("peril fusion", "rejected", True), ("pleural fusion", "auto_approved", False))])
        session.add(PotentialLexiconTerm(tenant_id=lab, surface_text="mosaic perfusion", normalized_text="mosaic perfusion", term_type="pathology", frequency=3, contexts=["Patient Ram Kumar shows mosaic perfusion."], status="approved"))
        if share:
            system_config.set_value(session, "training.share_approvals", 1, actor_id=reviewer, tenant_id=lab)


def test_export(migrated_db: str, two_tenants, tmp_path) -> None:
    lab, other = two_tenants
    _seed(migrated_db, lab, share=True)
    _seed(migrated_db, other, share=False)
    result = training_data.export(lab_sessions([lab, other], url=migrated_db), tmp_path)
    assert result.skipped_labs == (str(other),), "a lab that did not agree is not exported"
    assert {k: sum(v.values()) for k, v in result.counts.items()} == {"template_parse": 1, "variant_pairs": 2, "new_terms": 1}

    rows = {name: [json.loads(line) for half in ("train", "eval") for line in (tmp_path / f"{name}.{half}.jsonl").read_text().splitlines()] for name in result.counts}
    chat = rows["template_parse"][0]["messages"]
    assert [m["role"] for m in chat] == ["system", "user", "assistant"] and "Lungs:" in chat[1]["content"]
    answer = json.loads(chat[2]["content"])
    assert [f["label"] for f in answer["fields"]][:2] == ["Lungs", "Pleura"] and answer["fields"][0]["enum_values"] == ["clear", "consolidation", "ground glass"]
    assert {(r["heard"], r["label"]) for r in rows["variant_pairs"]} == {("plural effusion", "same"), ("peril fusion", "different")}, "automatic approvals are not labels"
    everything = "".join((tmp_path / f).read_text() for f in [p.name for p in tmp_path.iterdir()])
    assert "Ram Kumar" not in everything, "report sentences never leave"
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["labs"] == [str(lab)] and manifest["skipped_labs_without_consent"] == [str(other)]
    assert all(training_data.split(r["id"]) == training_data.split(r["id"]) for r in rows["variant_pairs"])
