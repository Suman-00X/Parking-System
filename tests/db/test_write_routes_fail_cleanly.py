"""Every write route, given empty, wrong-typed or unknown input by a caller allowed to use it, answers 4xx: never a server error."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from radreport.api.app import create_app
from tests.db.helpers import lab_headers, make_platform_user, signed_in

pytestmark = pytest.mark.db

POLICY = Path("radreport/api/access_policy.xml").read_text()
ROUTE = re.compile(r'<route id="([^"]+)" method="(POST|PUT|PATCH|DELETE)" path="([^"]+)" roles="([^"]*)"[^>]*?(?:/>|>(.*?)</route>)', re.S)
PARAM = re.compile(r'<param name="([^"]+)" in="([^"]+)" type="([^"]+)"')
WRONG = {"string": 12345, "int": "not-a-number", "number": "NaN-ish", "bool": "maybe", "uuid": "not-a-uuid", "array": "not-a-list", "object": [1, 2]}
SKIP = {"admin.login", "admin.logout", "auth.login", "auth.refresh", "auth.logout", "ui.login", "ui.logout"}


def _routes() -> list[tuple[str, str, str, list[str], list[tuple[str, str, str]]]]:
    out = []
    for rid, method, path, roles, body in ROUTE.findall(POLICY):
        if rid in SKIP or not roles or roles == "public":
            continue
        params = [p for p in PARAM.findall(body or "") if p[1] != "path"]
        out.append((rid, method, path, roles.split(","), params))
    return out


ROUTES = _routes()


@pytest.fixture(scope="module")
def callers(migrated_db: str, request):  # type: ignore[no-untyped-def]
    from radreport.db.models.tenancy import Tenant
    from radreport.db.session import system_session

    with system_session(migrated_db) as session:
        tenant = Tenant(name="write-probe", slug=f"write-probe-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        lab = tenant.id
    from sqlalchemy import text

    def fresh_limits() -> None:
        # Six sign-ins in a row would trip the five-a-minute sign-in limit.
        with system_session(migrated_db) as session:
            session.execute(text("DELETE FROM rate_limit_counter"))

    admin = signed_in(make_platform_user(migrated_db))
    admin.headers["Origin"] = "http://testserver"
    support = signed_in(make_platform_user(migrated_db, role="support"))
    fresh_limits()
    support.headers["Origin"] = "http://testserver"
    lab_client = TestClient(create_app(), follow_redirects=False)
    headers = {}
    for role in ("radiologist", "lab_admin", "transcriptionist", "auditor"):
        headers[role] = lab_headers(migrated_db, lab, role)
        fresh_limits()
    yield {"lab": lab, "admin": admin, "support": support, "lab_client": lab_client, "headers": headers}
    from tests.conftest import _purge_tenants

    _purge_tenants(migrated_db, [lab])


def _send(callers, method: str, path: str, roles: list[str], payload: dict, kind: str):  # type: ignore[no-untyped-def]
    url = re.sub(r"\{tenant_id\}", str(callers["lab"]), path)
    url = re.sub(r"\{key\}", "lexicon.review_above", url)
    url = re.sub(r"\{step\}", "derive-map", url)
    url = re.sub(r"\{class_code\}", "NO_SUCH_CLASS", url)
    url = re.sub(r"\{[a-z_]+\}", str(uuid.uuid4()), url)
    if path.startswith("/admin"):
        client, headers = (callers["admin"] if "product_admin" in roles else callers["support"]), {}
    else:
        role = next(r for r in ("radiologist", "lab_admin", "transcriptionist", "auditor") if r in roles)
        client, headers = callers["lab_client"], callers["headers"][role]
    if kind == "form":
        return client.request(method, url, data=payload, headers=headers)
    if kind == "file":
        return client.request(method, url, files={"file": ("x.bin", b"\x00\x01", "application/octet-stream")}, headers=headers)
    return client.request(method, url, json=payload, headers=headers)


@pytest.mark.parametrize(("rid", "method", "path", "roles", "params"), ROUTES, ids=[r[0] for r in ROUTES])
def test_bad_input_is_a_client_error(callers, rid, method, path, roles, params) -> None:  # type: ignore[no-untyped-def]
    kinds = {p[1] for p in params} or {"json"}
    kind = "file" if "file" in kinds else ("form" if "form" in kinds else "json")
    probes = [{}, {name: WRONG.get(kind_, "x") for name, where, kind_ in params if where in ("json", "form")}, {"unexpected_field": "x"}]
    for payload in probes:
        response = _send(callers, method, path, roles, payload, kind)
        assert response.status_code < 500, f"{rid} {method} {path} with {payload!r}: {response.status_code} {response.text[:300]}"
        assert response.status_code != 401, f"{rid}: the probe was not signed in, so it tested nothing"
