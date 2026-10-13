"""Gold bundles per template, from the uploaded ZIP to the frozen acceptance set the pilot gate reads."""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid
import zipfile

import pytest

from radreport.adapters.storage.object_store import InMemoryObjectStore
from radreport.admin import onboarding_steps
from radreport.admin.onboarding_steps import StepRefused
from radreport.core.types import CaptureDeviceClass, MatchMethod, UserRole
from radreport.db.models.evaluation import EvalItem, EvalSet
from radreport.db.models.identity import AppUser, RadiologistProfile
from radreport.db.models.knowledge import Template, TemplateVersion
from radreport.db.models.onboarding import CorpusReport, CorpusReportTemplateMap
from radreport.db.session import tenant_session
from radreport.devtools.synthetic import synth_audio
from radreport.knowledge.phonetics import double_metaphone
from radreport.onboarding.readiness import check_gold_bundles_per_template, load_facts

pytestmark = pytest.mark.db


def _lab(db: str, tenant_id: uuid.UUID, codes: tuple[str, ...]) -> list[str]:
    """Approved, live templates with these codes and three radiologists; returns their employee codes."""
    with tenant_session(tenant_id, url=db) as session:
        approver = AppUser(tenant_id=tenant_id, employee_code=f"A-{uuid.uuid4().hex[:6]}", display_name="Approver", roles=[UserRole.RADIOLOGIST])
        session.add(approver)
        session.flush()
        employee_codes = []
        for i in range(3):
            code = f"R{i}-{uuid.uuid4().hex[:6]}"
            user = AppUser(tenant_id=tenant_id, employee_code=code, display_name=f"Dr {i}", roles=[UserRole.RADIOLOGIST])
            session.add(user)
            session.flush()
            session.add(RadiologistProfile(tenant_id=tenant_id, user_id=user.id))
            employee_codes.append(code)
        for code in codes:
            template = Template(tenant_id=tenant_id, code=code, display_name=code.replace("_", " ").title(), modality="CT", body_region="chest")
            session.add(template)
            session.flush()
            spoken = code.replace("_", " ").lower()
            session.add(TemplateVersion(tenant_id=tenant_id, template_id=template.id, version=1, json_schema={"type": "object", "properties": {}}, render_spec={}, routing_card=spoken, trigger_rules={}, spoken_study_code=spoken, spoken_study_code_phonetic=double_metaphone(spoken)[0], approved_by=approver.id, effective_from=dt.datetime.now(dt.UTC), is_current=True))
        session.flush()
    return employee_codes


def _zip(rows: list[dict[str, str]], *, skip_audio: str | None = None) -> bytes:
    """A gold-bundle ZIP: the manifest plus one synthetic dictation per row."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        manifest = io.StringIO()
        writer = csv.DictWriter(manifest, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        archive.writestr("bundles/manifest.csv", manifest.getvalue())
        for i, row in enumerate(rows):
            if row["audio_file"] != skip_audio:
                archive.writestr(f"bundles/{row['audio_file']}", synth_audio(seconds=4, seed=hash(row["external_report_id"]) % 10_000 + i))
    return out.getvalue()


def _rows(codes: tuple[str, ...], radiologists: list[str], per: int) -> list[dict[str, str]]:
    return [{"template_code": code, "audio_file": f"{code}-{n}.flac", "verbatim_text": "um the lungs are are clear", "report_text": "Lungs: Clear.\nImpression: Normal study.", "radiologist_employee_code": radiologists[n % len(radiologists)], "external_report_id": f"{code}-{n}-{uuid.uuid4().hex[:6]}"} for code in codes for n in range(per)]


def test_bundles_fill_every_template_and_freeze_passes_the_check(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    codes = ("CT_CHEST", "CT_ABDOMEN")
    radiologists = _lab(migrated_db, tenant_id, codes)
    store = InMemoryObjectStore()

    with tenant_session(tenant_id, url=migrated_db) as session:
        assert check_gold_bundles_per_template(load_facts(session, tenant_id)).status == "fail"
        result = onboarding_steps.import_gold_bundle_file(session, tenant_id, _zip(_rows(codes, radiologists, 4)), "bundles.zip", store=store)
        assert result["accepted"] == 8 and result["per_template"] == {"CT_CHEST": 4, "CT_ABDOMEN": 4} and not result["rejected"]

        # The lab named each report's template, so its mapping is already verified, not a guess.
        maps = session.query(CorpusReportTemplateMap).join(CorpusReport, CorpusReport.id == CorpusReportTemplateMap.corpus_report_id).filter(CorpusReport.tenant_id == tenant_id).all()
        assert len(maps) == 8 and all(m.is_verified and m.match_method == MatchMethod.HUMAN for m in maps)

        assembled = onboarding_steps.run_step(session, tenant_id, "acceptance-assemble")
        assert assembled["items"] == 8 and assembled["short"] == {}
        onboarding_steps.run_step(session, tenant_id, "acceptance-freeze")
        outcome = check_gold_bundles_per_template(load_facts(session, tenant_id))
        assert outcome.status == "pass", outcome.detail
        assert session.query(EvalItem).filter(EvalItem.gold_template_version_id.is_(None), EvalItem.source_tenant_id == tenant_id).count() == 0


def test_a_template_short_of_bundles_blocks_the_freeze(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    radiologists = _lab(migrated_db, tenant_id, ("CT_CHEST", "CT_HEAD"))
    with tenant_session(tenant_id, url=migrated_db) as session:
        onboarding_steps.import_gold_bundle_file(session, tenant_id, _zip(_rows(("CT_CHEST",), radiologists, 4) + _rows(("CT_HEAD",), radiologists, 2)), "bundles.zip", store=InMemoryObjectStore())
        assembled = onboarding_steps.run_step(session, tenant_id, "acceptance-assemble")
        assert assembled["short"] == {"CT_HEAD": 2}
        with pytest.raises(StepRefused, match="CT_HEAD"):
            onboarding_steps.run_step(session, tenant_id, "acceptance-freeze")
        outcome = check_gold_bundles_per_template(load_facts(session, tenant_id))
        assert outcome.status == "fail"


def test_a_bad_bundle_is_rejected_alone_and_a_resend_reuses_what_was_stored(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    radiologists = _lab(migrated_db, tenant_id, ("CT_CHEST",))
    rows = _rows(("CT_CHEST",), radiologists, 3)
    rows[1] = {**rows[1], "template_code": "NO_SUCH_TEMPLATE"}
    rows[2] = {**rows[2], "radiologist_employee_code": "NOBODY"}
    store = InMemoryObjectStore()
    data = _zip(rows)
    with tenant_session(tenant_id, url=migrated_db) as session:
        first = onboarding_steps.import_gold_bundle_file(session, tenant_id, data, "bundles.zip", store=store)
        assert first["accepted"] == 1 and len(first["rejected"]) == 2
        again = onboarding_steps.import_gold_bundle_file(session, tenant_id, data, "bundles.zip", store=store)
        assert again["accepted"] == 1 and again["reused"] == 1


def test_a_manifest_naming_missing_audio_is_reported_per_row(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    radiologists = _lab(migrated_db, tenant_id, ("CT_CHEST",))
    rows = _rows(("CT_CHEST",), radiologists, 2)
    with tenant_session(tenant_id, url=migrated_db) as session:
        result = onboarding_steps.import_gold_bundle_file(session, tenant_id, _zip(rows, skip_audio=rows[0]["audio_file"]), "bundles.zip", store=InMemoryObjectStore())
        assert result["accepted"] == 1 and "is not in the ZIP" in result["problems"][0]
        with pytest.raises(StepRefused, match="not a ZIP"):
            onboarding_steps.import_gold_bundle_file(session, tenant_id, b"plain text", "bundles.zip", store=InMemoryObjectStore())


def test_archive_audio_never_fills_the_acceptance_set(migrated_db: str, two_tenants) -> None:
    """The pilot question is about the lab's new hardware; legacy recordings cannot answer it."""
    tenant_id, _ = two_tenants
    radiologists = _lab(migrated_db, tenant_id, ("CT_CHEST",))
    rows = [{**r, "capture_device_class": CaptureDeviceClass.LEGACY} for r in _rows(("CT_CHEST",), radiologists, 4)]
    with tenant_session(tenant_id, url=migrated_db) as session:
        onboarding_steps.import_gold_bundle_file(session, tenant_id, _zip(rows), "bundles.zip", store=InMemoryObjectStore())
        assert onboarding_steps.run_step(session, tenant_id, "acceptance-assemble")["items"] == 0


def test_a_frozen_set_is_final_and_the_next_one_starts_empty(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    radiologists = _lab(migrated_db, tenant_id, ("CT_CHEST",))
    with tenant_session(tenant_id, url=migrated_db) as session:
        assert session.query(EvalSet).filter_by(tenant_id=tenant_id).count() == 0
        onboarding_steps.import_gold_bundle_file(session, tenant_id, _zip(_rows(("CT_CHEST",), radiologists, 4)), "bundles.zip", store=InMemoryObjectStore())
        onboarding_steps.run_step(session, tenant_id, "acceptance-assemble")
        onboarding_steps.run_step(session, tenant_id, "acceptance-freeze")
        # A template added after the freeze is tested in the next version, which starts from every bundle again.
        again = onboarding_steps.run_step(session, tenant_id, "acceptance-assemble")
        assert again["items"] == 4
        sets = session.query(EvalSet).filter_by(tenant_id=tenant_id, is_canonical=False).order_by(EvalSet.created_at).all()
        assert [s.is_frozen for s in sets] == [True, False]
