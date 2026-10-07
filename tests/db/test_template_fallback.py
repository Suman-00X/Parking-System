"""An unsure template parse is also read by the lab's template model; a confident one, or a lab without a model, is not."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from radreport.db.models.onboarding import ImportArtifact
from radreport.db.session import tenant_session
from radreport.devtools.template_eval import FIXTURES
from radreport.onboarding import templates
from radreport.onboarding.batches import ArtifactUpload
from tests.unit.test_template_llm import _model

pytestmark = pytest.mark.db


def test_unsure_uploads_go_to_the_model(migrated_db: str, two_tenants) -> None:
    lab, _ = two_tenants
    prose = (FIXTURES / "bullets_xray_chest.txt").read_bytes()
    clean = (FIXTURES / "structured_usg_abdomen.txt").read_bytes()
    stand_in = _model({"bullets xray chest": [{"label": "Trachea", "section": "FINDINGS", "data_type": "text"}, {"label": "Costophrenic angles", "section": "FINDINGS", "data_type": "enum", "enum_values": ["sharp", "blunted"]}]})
    with tenant_session(lab, url=migrated_db) as session:
        submission = templates.submit_templates(session, tenant_id=lab, uploads=[ArtifactUpload("bullets_xray_chest.txt", prose), ArtifactUpload("structured_usg_abdomen.txt", clean)], fallback=stand_in)
        assert submission.model_assisted == ["bullets_xray_chest.txt"], "a confident parse is not sent"
        by_name = {c.proposed_code: c for c in submission.candidates}
        artifacts = {a.original_filename: a for a in session.execute(select(ImportArtifact).where(ImportArtifact.import_batch_id == submission.batch.id)).scalars()}
        audit = artifacts["bullets_xray_chest.txt"].parse_warnings["template_model"]
        assert audit["used"] and audit["fields_added"] == 2 and audit["parser_confidence"] == 0.0 and audit["model"] == "stand-in"
        assert "template_model" not in (artifacts["structured_usg_abdomen.txt"].parse_warnings or {})
        assisted = next(c for c in by_name.values() if "costophrenic_angles" in str(c.proposed_json_schema))
        assert 0.5 < float(assisted.parse_confidence) <= 0.85

        # Without an assigned template_parse model the parser's result stands.
        alone = templates.submit_templates(session, tenant_id=lab, uploads=[ArtifactUpload("prose_mri_brain.txt", (FIXTURES / "prose_mri_brain.txt").read_bytes())])
        assert alone.model_assisted == [] and float(alone.candidates[0].parse_confidence) == 0.0


def test_model_read_templates_are_checked_field_by_field(migrated_db: str, two_tenants) -> None:
    from radreport.onboarding.templates import model_fields_added

    lab, _ = two_tenants
    stand_in = _model({"table ct kub": [{"label": "Ureters", "section": "FINDINGS", "data_type": "enum", "enum_values": ["dilated", "not dilated"]}]})
    with tenant_session(lab, url=migrated_db) as session:
        submission = templates.submit_templates(session, tenant_id=lab, uploads=[ArtifactUpload("table_ct_kub.txt", (FIXTURES / "table_ct_kub.txt").read_bytes())], fallback=stand_in)
        candidate = submission.candidates[0]
        assert model_fields_added(session, [candidate.import_artifact_id]) == {candidate.import_artifact_id: 1}
