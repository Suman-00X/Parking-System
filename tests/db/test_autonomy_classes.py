"""A product admin defines an autonomy class with its measured baseline; the accrual and lab autonomy routes then answer for it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from radreport.api.app import create_app
from radreport.db.models.knowledge import Template
from radreport.db.session import tenant_session
from tests.db.helpers import lab_headers, make_platform_user, signed_in

pytestmark = pytest.mark.db


def test_define_list_and_use_a_class(migrated_db: str, two_tenants) -> None:
    lab, _ = two_tenants
    with tenant_session(lab, url=migrated_db) as session:
        session.add_all([Template(tenant_id=lab, code=code, display_name=code.replace("_", " ").title(), modality="US", body_region="abdomen") for code in ("US_ABDOMEN", "US_PELVIS")])
    admin = signed_in(make_platform_user(migrated_db))
    admin.headers["Origin"] = "http://testserver"
    base = f"/admin/api/labs/{lab}/autonomy-classes"
    body = {"code": "ROUTINE_US", "display_name": "Routine ultrasound", "baseline_cse_rate": 0.025, "required_n": 200, "template_codes": ["US_ABDOMEN", "US_PELVIS"]}
    assert admin.post(base, json={**body, "baseline_cse_rate": 0}).status_code in (400, 422), "an unmeasured baseline is refused"
    assert admin.post(base, json={**body, "template_codes": ["NOPE"]}).status_code == 422
    assert admin.post(base, json=body).status_code == 201
    listed = admin.get(base).json()
    assert listed == [{"code": "ROUTINE_US", "display_name": "Routine ultrasound", "status": "not_evaluated", "baseline_cse_rate": 0.025, "ni_margin_pp": 1.0, "required_n": 200, "accrued_n": 0, "templates": ["US_ABDOMEN", "US_PELVIS"]}]
    assert admin.get(f"/admin/api/labs/{lab}/autonomy/ROUTINE_US").status_code == 200, "the accrual read now finds the class"
    assert admin.post(f"/admin/api/labs/{lab}/autonomy/ROUTINE_US/open-accrual").status_code == 200
    assert admin.post(base, json={**body, "baseline_cse_rate": 0.05}).status_code == 409, "the baseline is fixed once evidence is collected"
    lab_view = TestClient(create_app()).get("/ga/autonomy/ROUTINE_US", headers=lab_headers(migrated_db, lab, "radiologist"))
    assert lab_view.status_code == 200
