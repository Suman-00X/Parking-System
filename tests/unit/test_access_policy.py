"""The access policy file and the middleware that enforces it, without a database."""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from radreport.admin.auth import SESSION_COOKIE, AuthenticatedAdmin
from radreport.api.access import AccessMiddleware, PolicyError, RateLimit, RateLimiter, load_policy, parse_policy, verify_coverage
from radreport.api.app import create_app
from radreport.auth.lab import issue_access_token
from radreport.core.types import PlatformRole, UserRole

ROLES = """
  <roles>
    <role id="product_admin" realm="admin"/>
    <role id="support" realm="admin"/>
    <role id="radiologist" realm="lab"/>
    <role id="auditor" realm="lab"/>
  </roles>
  <rate-limits>
    <rate-limit id="tight" requests="2" window-seconds="60" key="principal"/>
    <rate-limit id="ip" requests="100" window-seconds="60" key="ip"/>
  </rate-limits>
"""

POLICY = f"""<access-policy version="1">{ROLES}
  <routes realm="public">
    <route id="open" method="GET" path="/open" rate-limit="ip"/>
    <route id="devonly" method="GET" path="/devonly" environments="nowhere"/>
  </routes>
  <routes realm="admin">
    <route id="panel" method="GET" path="/admin/things" roles="product_admin, support"/>
    <route id="panel.write" method="POST" path="/admin/things" roles="product_admin" max-body-bytes="10"/>
    <route id="api" method="GET" path="/admin/api/things/{{thing_id}}" roles="product_admin" rate-limit="tight">
      <param name="thing_id" in="path" required="true"/>
    </route>
  </routes>
  <routes realm="lab">
    <route id="sign" method="POST" path="/lab/sign" roles="radiologist"/>
  </routes>
</access-policy>"""

ADMIN_TOKEN, SUPPORT_TOKEN = "admin-token", "support-token"
LAB_TENANT, RADIOLOGIST, AUDITOR = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _admin(token: str | None) -> AuthenticatedAdmin | None:
    role = {ADMIN_TOKEN: PlatformRole.PRODUCT_ADMIN, SUPPORT_TOKEN: PlatformRole.SUPPORT}.get(token or "")
    return AuthenticatedAdmin(platform_user_id=uuid.uuid5(uuid.NAMESPACE_DNS, role), display_name=role, role=role, session_id=uuid.uuid4()) if role else None


def _bearer(user_id: uuid.UUID, *roles: str) -> dict[str, str]:
    token, _ = issue_access_token(user_id=user_id, tenant_id=LAB_TENANT, roles=list(roles))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    for method, path in (("GET", "/open"), ("GET", "/devonly"), ("GET", "/admin/things"), ("POST", "/admin/things"), ("GET", "/admin/api/things/{thing_id}"), ("POST", "/lab/sign"), ("GET", "/unlisted")):
        app.add_api_route(path, lambda: {"ok": True}, methods=[method])
    app.add_middleware(AccessMiddleware, policy=parse_policy(POLICY), admin_resolver=_admin)
    return TestClient(app, follow_redirects=False)


# ================================================================ parsing ===
def _policy(routes: str) -> str:
    return f'<access-policy version="1">{ROLES}{routes}</access-policy>'


@pytest.mark.parametrize(
    ("routes", "message"),
    [
        ('<routes realm="admin"><route id="a" method="GET" path="/a" roles="nobody"/></routes>', "not a role of the admin realm"),
        ('<routes realm="admin"><route id="a" method="GET" path="/a" roles="support,radiologist"/></routes>', "not a role of the admin realm"),
        ('<routes realm="admin"><route id="a" method="GET" path="/a"/></routes>', "allows nobody"),
        ('<routes realm="public"><route id="a" method="GET" path="/a" roles="support"/></routes>', "takes no roles"),
        ('<routes realm="public"><route id="a" method="GET" path="/a/{x}"/></routes>', "does not declare its path parameter"),
        ('<routes realm="public"><route id="a" method="GET" path="/a"><param name="q" in="query" pattern="("/></route></routes>', "not a valid regex"),
        ('<routes realm="public"><route id="a" method="GET" path="/a"><param name="q" in="body"/></route></routes>', "in= one of"),
        ('<routes realm="public"><route id="a" method="POST" path="/a"><param name="f" in="file"/></route></routes>', "type=file goes with in=file"),
        ('<routes realm="public"><route id="a" method="POST" path="/a"><param name="q" in="query"/><param name="q" in="query"/></route></routes>', "declares a parameter twice"),
        ('<routes realm="public"><route id="a" method="POST" path="/a"><param name="j" in="json"/><param name="f" in="form"/></route></routes>', "mixes a JSON body"),
        ('<routes realm="public"><route id="a" method="GET" path="/a"/><route id="b" method="GET" path="/a"/></routes>', "listed twice"),
        ('<routes realm="public"><route id="a" method="GET" path="/a"/><route id="a" method="GET" path="/b"/></routes>', "used twice"),
        ('<routes realm="public"><route id="a" method="GET" path="/a" rate-limit="missing"/></routes>', "unknown rate limit"),
        ('<routes realm="public"><route id="a" method="GET" path="/a" rate-limit="tight"/></routes>', "keyed by ip"),
        ('<routes realm="nowhere"><route id="a" method="GET" path="/a"/></routes>', "must be one of"),
    ],
)
def test_a_malformed_policy_is_refused(routes: str, message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        parse_policy(_policy(routes))


def test_paths_match_their_parameters_and_head_follows_get() -> None:
    policy = parse_policy(POLICY)
    assert policy.match("GET", "/admin/api/things/42").id == "api"
    assert policy.match("HEAD", "/admin/api/things/42").id == "api"
    assert policy.match("GET", "/admin/api/things/42/more") is None
    assert policy.match("DELETE", "/admin/things") is None


# ======================================================= the shipped file ===
def test_the_shipped_policy_covers_exactly_the_served_routes() -> None:
    """create_app already refuses to start otherwise; this names the check."""
    verify_coverage(create_app(), load_policy())


def test_support_is_read_only() -> None:
    policy = load_policy()
    for rule in policy.routes:
        if PlatformRole.SUPPORT in rule.roles:
            assert rule.method == "GET", f"{rule.id} lets support change something"


def test_every_admin_route_requires_a_product_admin_or_support_session() -> None:
    policy = load_policy()
    public_admin = {r.path for r in policy.routes if r.realm == "public" and r.path.startswith("/admin")}
    assert public_admin == {"/admin/login", "/admin/logout"}
    for rule in policy.routes:
        if rule.path.startswith("/admin") and rule.realm != "public":
            assert rule.realm == "admin"
            assert rule.roles <= {PlatformRole.PRODUCT_ADMIN, PlatformRole.SUPPORT}


def test_clinical_approvals_stay_with_radiologists() -> None:
    policy = load_policy()
    for rule_id in ("onboarding.candidate.review", "onboarding.merge.decide", "onboarding.batch.apply", "onboarding.rule.approve", "onboarding.finding.resolve", "review.draft.sign"):
        rule = next(r for r in policy.routes if r.id == rule_id)
        assert rule.roles == {UserRole.RADIOLOGIST}, rule_id


def test_every_write_has_a_rate_limit_and_a_body_cap() -> None:
    for rule in load_policy().routes:
        if rule.method != "GET":
            assert rule.rate_limit is not None, rule.id
            assert rule.max_body_bytes is not None, rule.id


# ============================================================ rate limit ===
def test_the_limiter_refuses_past_the_limit_and_recovers_after_the_window() -> None:
    now = [1000.0]
    limiter = RateLimiter(clock=lambda: now[0])
    limit = RateLimit(id="t", requests=2, window_seconds=60, key="principal")

    assert limiter.hit(limit, "a") is None
    assert limiter.hit(limit, "a") is None
    assert limiter.hit(limit, "a") == pytest.approx(60.0)
    assert limiter.hit(limit, "b") is None, "callers are counted separately"

    now[0] += 61
    assert limiter.hit(limit, "a") is None


# ============================================================ middleware ===
def test_an_unlisted_route_is_refused_even_though_it_is_served(client: TestClient) -> None:
    assert client.get("/unlisted").status_code == 404


def test_a_public_route_needs_no_identity(client: TestClient) -> None:
    assert client.get("/open").status_code == 200


def test_an_environment_gated_route_is_hidden_elsewhere(client: TestClient) -> None:
    assert client.get("/devonly").status_code == 404


def test_a_signed_out_browser_is_sent_to_sign_in_and_an_api_call_gets_401(client: TestClient) -> None:
    page = client.get("/admin/things")
    assert page.status_code == 303
    assert page.headers["location"] == "/admin/login"
    assert client.get("/admin/api/things/1").status_code == 401
    assert client.post("/admin/things").status_code == 401


def test_support_can_read_but_not_write(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, SUPPORT_TOKEN)
    assert client.get("/admin/things").status_code == 200
    assert client.post("/admin/things").status_code == 403
    assert client.get("/admin/api/things/1").status_code == 403


def test_a_product_admin_can_write(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, ADMIN_TOKEN)
    assert client.post("/admin/things").status_code == 200


def test_a_body_over_the_cap_is_refused_before_the_handler(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, ADMIN_TOKEN)
    assert client.post("/admin/things", content=b"x" * 11).status_code == 413


def test_the_rate_limit_answers_429_with_retry_after(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, ADMIN_TOKEN)
    assert client.get("/admin/api/things/1").status_code == 200
    assert client.get("/admin/api/things/2").status_code == 200
    refused = client.get("/admin/api/things/3")
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) >= 1


def test_a_lab_route_needs_a_valid_bearer_token_with_the_role(client: TestClient) -> None:
    assert client.post("/lab/sign").status_code == 401
    assert client.post("/lab/sign", headers={"Authorization": "Bearer not-a-token"}).status_code == 401
    assert client.post("/lab/sign", headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.post("/lab/sign", headers=_bearer(AUDITOR, UserRole.AUDITOR)).status_code == 403
    assert client.post("/lab/sign", headers=_bearer(RADIOLOGIST, UserRole.RADIOLOGIST)).status_code == 200


def test_the_old_identity_headers_grant_nothing(client: TestClient) -> None:
    assert client.post("/lab/sign", headers={"X-User-Id": str(RADIOLOGIST), "X-Tenant-Id": str(LAB_TENANT)}).status_code == 401


def test_an_admin_cookie_does_not_open_a_lab_route(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, ADMIN_TOKEN)
    assert client.post("/lab/sign").status_code == 401


# ======================================================= the real app ===
def test_the_real_app_refuses_signed_out_admin_and_lab_calls() -> None:
    client = TestClient(create_app(), follow_redirects=False)
    assert client.get("/admin/labs").headers["location"] == "/admin/login"
    assert client.get("/admin/api/labs").status_code == 401
    assert client.post("/admin/api/labs", json={}).status_code == 401
    assert client.get("/review/queue").status_code == 401
    assert client.get("/admin/login").status_code == 200


def test_the_old_routes_are_gone() -> None:
    client = TestClient(create_app(), follow_redirects=False)
    for path in ("/console/labs", "/admin/tenants", "/onboarding/roster", "/onboarding/status", "/ga/autonomy/x/grant"):
        assert client.get(path).status_code == 404, path


def test_the_step_routes_accept_exactly_the_steps_the_code_runs() -> None:
    """The step name is a path parameter whose pattern repeats `STEPS`; the two must not drift."""
    from radreport.admin.onboarding_steps import STEPS

    for rule_id in ("admin.lab.onboarding.step", "admin.api.lab.onboarding.step"):
        rule = next(r for r in load_policy().routes if r.id == rule_id)
        step = rule.param("path", "step")
        assert step is not None and step.pattern is not None
        listed = set(step.pattern.pattern.replace("\\-", "-").split("|"))
        assert listed == set(STEPS), f"{rule_id}: policy lists {sorted(listed ^ set(STEPS))} differently from STEPS"
