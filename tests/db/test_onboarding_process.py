"""The nine-step onboarding's newer parts through the admin API: the critical-findings switch, the baseline audit, the accuracy check, and reports that name their template."""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid

import pytest

from radreport.core import system_config
from radreport.db.models.evaluation import EvalRun, EvalSet
from radreport.db.models.onboarding import CorpusReport, CorpusReportTemplateMap
from radreport.db.session import tenant_session
from radreport.onboarding.critical_rules import alerts_enabled
from radreport.onboarding.readiness import check_acceptance_accuracy, check_baseline_cse_measured, load_facts
from radreport.pipeline.stages.providers import load_tenant_knowledge
from tests.db.helpers import make_platform_user, signed_in
from tests.db.test_acceptance_set import _lab

pytestmark = pytest.mark.db


def _reports(code: str, n: int) -> list[dict[str, object]]:
    return [{"report_text": f"Liver: Normal in size.\nImpression: Normal study {i}.", "external_report_id": f"{code}-{i}-{uuid.uuid4().hex[:6]}", "template_code": code, "is_deidentified": True} for i in range(n)]


# ============================================================ critical switch ===
def test_alerts_are_off_until_a_product_admin_switches_them_on(migrated_db: str, two_tenants) -> None:
    tenant_id, other = two_tenants
    client = signed_in(make_platform_user(migrated_db))
    with tenant_session(tenant_id, url=migrated_db) as session:
        assert alerts_enabled(session, tenant_id) is False
        assert load_tenant_knowledge(session, tenant_id).critical_alerts_on is False

    assert client.post(f"/admin/api/labs/{tenant_id}/onboarding/critical-alerts", json={"enabled": True}).json() == {"enabled": True, "source": "lab"}
    with tenant_session(tenant_id, url=migrated_db) as session:
        assert load_tenant_knowledge(session, tenant_id).critical_alerts_on is True
    with tenant_session(other, url=migrated_db) as session:
        assert alerts_enabled(session, other) is False, "one lab's switch is not another's"

    page = client.post(f"/admin/labs/{tenant_id}/onboarding/critical-alerts", data={"enabled": "0"})
    assert page.status_code == 303 and "switched+off" in page.headers["location"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        assert alerts_enabled(session, tenant_id) is False


# ============================================================= baseline audit ===
def test_the_baseline_comes_from_grades_and_only_once_there_are_enough(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    _lab(migrated_db, tenant_id, ("US_ABDOMEN",))
    client = signed_in(make_platform_user(migrated_db))
    with tenant_session(tenant_id, url=migrated_db) as session:
        system_config.set_value(session, "autonomy.baseline_min_graded", 30, actor_id=None, tenant_id=tenant_id)

    defined = client.post(f"/admin/api/labs/{tenant_id}/autonomy-classes", json={"code": "ROUTINE_US", "display_name": "Routine ultrasound", "required_n": 100, "template_codes": ["US_ABDOMEN"]})
    assert defined.status_code == 201, defined.text
    loaded = client.post(f"/admin/api/labs/{tenant_id}/onboarding/corpus", json={"records": _reports("US_ABDOMEN", 40)}).json()
    assert loaded["loaded"] == 40 and loaded["mapped"] == 40

    with tenant_session(tenant_id, url=migrated_db) as session:
        assert "ROUTINE_US" in check_baseline_cse_measured(load_facts(session, tenant_id)).detail["classes_without_baseline"]
    assert client.post(f"/admin/api/labs/{tenant_id}/autonomy/ROUTINE_US/baseline").status_code == 409, "no grades yet"

    sample = client.get(f"/admin/api/labs/{tenant_id}/autonomy/ROUTINE_US/baseline-sample")
    assert sample.status_code == 200 and sample.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(sample.text)))
    assert len(rows) == 30, "exactly what the audit still needs"

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["external_report_id", "severity_grade", "grader"])
    writer.writeheader()
    for i, row in enumerate(rows):
        writer.writerow({"external_report_id": row["external_report_id"], "severity_grade": "G3" if i == 0 else "G0", "grader": "Dr Outside"})
    graded = client.post(f"/admin/api/labs/{tenant_id}/autonomy/ROUTINE_US/baseline-grades", files={"file": ("grades.csv", out.getvalue().encode(), "text/csv")}).json()
    assert graded["recorded"] == 30 and not graded["rejected"]

    measured = client.post(f"/admin/api/labs/{tenant_id}/autonomy/ROUTINE_US/baseline").json()
    # One error in thirty, as the Beta(0.5, 0.5) posterior mean: never zero, close to 1/30.
    assert measured["graded"] == 30 and measured["cse"] == 1 and measured["baseline_cse_rate"] == pytest.approx(1.5 / 31, abs=1e-5)
    with tenant_session(tenant_id, url=migrated_db) as session:
        assert check_baseline_cse_measured(load_facts(session, tenant_id)).status == "pass"

    assert client.post(f"/admin/api/labs/{tenant_id}/autonomy/ROUTINE_US/open-accrual").status_code == 200
    assert client.post(f"/admin/api/labs/{tenant_id}/autonomy/ROUTINE_US/baseline-grades", files={"file": ("grades.csv", out.getvalue().encode(), "text/csv")}).status_code == 409, "the baseline is fixed once evidence is collected"


def test_a_typed_baseline_with_no_grades_does_not_pass_readiness(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    _lab(migrated_db, tenant_id, ("US_PELVIS",))
    client = signed_in(make_platform_user(migrated_db))
    assert client.post(f"/admin/api/labs/{tenant_id}/autonomy-classes", json={"code": "PELVIS", "display_name": "Pelvis", "baseline_cse_rate": 0.02, "required_n": 100, "template_codes": ["US_PELVIS"]}).status_code == 201
    with tenant_session(tenant_id, url=migrated_db) as session:
        assert check_baseline_cse_measured(load_facts(session, tenant_id)).status == "fail"


# ============================================================ accuracy check ===
def test_accuracy_reads_the_latest_full_gate_run_on_the_frozen_set(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    now = dt.datetime.now(dt.UTC)
    with tenant_session(tenant_id, url=migrated_db) as session:
        assert check_acceptance_accuracy(load_facts(session, tenant_id)).status == "fail"
        eval_set = EvalSet(tenant_id=tenant_id, name=f"acc-{uuid.uuid4().hex[:6]}", is_frozen=True, is_canonical=False)
        session.add(eval_set)
        session.flush()
        good = {"CSE_DRAFT": 0.0, "HALLUC_RATE": 0.01, "ROUTE_TOP1": 0.95}
        session.add(EvalRun(tenant_id=tenant_id, eval_set_id=eval_set.id, pipeline_version="1", config_snapshot={}, is_release_gate=True, completed_at=now - dt.timedelta(minutes=2), metrics=good))
        session.flush()
        assert check_acceptance_accuracy(load_facts(session, tenant_id)).status == "pass"

        # A newer smoke run is ignored; a newer full run that misses a limit fails the check.
        session.add(EvalRun(tenant_id=tenant_id, eval_set_id=eval_set.id, pipeline_version="2", config_snapshot={}, is_release_gate=True, is_smoke_subset=True, completed_at=now - dt.timedelta(minutes=1), metrics={**good, "HALLUC_RATE": 0.5}))
        session.flush()
        assert check_acceptance_accuracy(load_facts(session, tenant_id)).status == "pass"
        session.add(EvalRun(tenant_id=tenant_id, eval_set_id=eval_set.id, pipeline_version="3", config_snapshot={}, is_release_gate=True, completed_at=now, metrics={**good, "HALLUC_RATE": 0.2}))
        session.flush()
        outcome = check_acceptance_accuracy(load_facts(session, tenant_id))
        assert outcome.status == "fail" and "HALLUC_RATE" in outcome.detail["regressions"][0]


# =================================================== reports naming templates ===
def test_a_report_naming_an_unknown_template_is_rejected_alone(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    _lab(migrated_db, tenant_id, ("CT_CHEST",))
    client = signed_in(make_platform_user(migrated_db))
    records = [*_reports("CT_CHEST", 2), *_reports("CT_NOPE", 1), {"report_text": "Free text only.", "external_report_id": f"x-{uuid.uuid4().hex[:6]}"}]
    loaded = client.post(f"/admin/api/labs/{tenant_id}/onboarding/corpus", json={"records": records}).json()
    assert loaded["loaded"] == 3 and loaded["mapped"] == 2 and "CT_NOPE" in loaded["rejected"][0]["reason"]
    with tenant_session(tenant_id, url=migrated_db) as session:
        verified = session.query(CorpusReportTemplateMap).join(CorpusReport, CorpusReport.id == CorpusReportTemplateMap.corpus_report_id).filter(CorpusReport.tenant_id == tenant_id, CorpusReportTemplateMap.is_verified.is_(True)).count()
        assert verified == 2


# ================================================================ the page ===
def test_the_onboarding_page_walks_the_nine_steps(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    client = signed_in(make_platform_user(migrated_db))
    page = client.get(f"/admin/labs/{tenant_id}/onboarding")
    assert page.status_code == 200
    for title in ("1. Register the lab", "4. Gold bundles per template", "7. Baseline audit", "9. Pilot, then live"):
        assert title in page.text
    assert "Switch alerts on" in page.text and f"/admin/labs/{tenant_id}/onboarding/gold-bundles" in page.text
