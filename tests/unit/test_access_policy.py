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


#: The only writes support may make: to its own account, never to configuration.
SUPPORT_OWN_ACCOUNT_ROUTES = {"admin.account.password", "admin.api.account.password"}


def test_support_is_read_only() -> None:
    policy = load_policy()
    for rule in policy.routes:
        if PlatformRole.SUPPORT in rule.roles and rule.id not in SUPPORT_OWN_ACCOUNT_ROUTES:
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


# ================================================================== csrf ===
def test_a_cross_site_admin_write_is_refused(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, ADMIN_TOKEN)
    assert client.post("/admin/things", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/admin/things", headers={"Referer": "https://evil.example/page"}).status_code == 403


def test_a_same_site_or_headerless_admin_write_is_allowed(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, ADMIN_TOKEN)
    assert client.post("/admin/things", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/admin/things", headers={"Referer": "http://testserver/admin/things"}).status_code == 200
    assert client.post("/admin/things").status_code == 200, "a script sends no Origin and cannot be a forged browser request"


def test_reads_and_lab_routes_skip_the_origin_check(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, ADMIN_TOKEN)
    assert client.get("/admin/things", headers={"Origin": "https://evil.example"}).status_code == 200
    bearer = _bearer(RADIOLOGIST, UserRole.RADIOLOGIST) | {"Origin": "https://evil.example"}
    assert client.post("/lab/sign", headers=bearer).status_code == 200, "a bearer token is never sent by the browser on its own"


def test_a_trusted_origin_is_allowed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from radreport.core.config import get_settings

    monkeypatch.setenv("RADREPORT_TRUSTED_ORIGINS", '["https://admin.radreport.example"]')
    get_settings.cache_clear()
    try:
        client.cookies.set(SESSION_COOKIE, ADMIN_TOKEN)
        assert client.post("/admin/things", headers={"Origin": "https://admin.radreport.example"}).status_code == 200
    finally:
        get_settings.cache_clear()


def test_a_forged_login_from_another_site_is_refused() -> None:
    """Login CSRF: signing a victim into the attacker's account is refused before any password check."""
    client = TestClient(create_app(), follow_redirects=False)
    response = client.post("/admin/login", data={"email": "a@b.c", "password": "x" * 12}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


# ========================================================== shared limits ===
def test_a_shared_limit_counts_in_fixed_windows() -> None:
    from radreport.api.access import SharedRateLimiter

    counts: dict[tuple[str, str, object], int] = {}

    def counter(limit_id: str, who: str, window_start: object) -> int:
        counts[(limit_id, who, window_start)] = counts.get((limit_id, who, window_start), 0) + 1
        return counts[(limit_id, who, window_start)]

    now = [1_000_040.0]  # 20 s into the window that starts at 1_000_020
    limiter = SharedRateLimiter(counter=counter, clock=lambda: now[0])
    limit = RateLimit(id="login", requests=2, window_seconds=60, key="ip", store="shared")
    assert limiter.hit(limit, "ip:1") is None
    assert limiter.hit(limit, "ip:1") is None
    assert limiter.hit(limit, "ip:1") == pytest.approx(40.0), "wait until the window ends"
    now[0] += 40
    assert limiter.hit(limit, "ip:1") is None, "a new window starts a new count"


def test_sign_in_limits_are_shared_and_throughput_limits_are_not() -> None:
    limits = load_policy().rate_limits
    assert {lid for lid, limit in limits.items() if limit.store == "shared"} == {"login", "token-refresh"}


def test_an_unknown_store_is_refused() -> None:
    with pytest.raises(PolicyError, match="store must be"):
        parse_policy('<access-policy version="1"><rate-limits><rate-limit id="x" requests="1" window-seconds="1" key="ip" store="redis"/></rate-limits></access-policy>')


# ======================================================= browser sign-in ===
def test_a_signed_out_browser_on_a_review_page_is_sent_to_sign_in() -> None:
    client = TestClient(create_app(), follow_redirects=False)
    response = client.get("/ui/queue")
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login?next=%2Fui%2Fqueue"


def test_an_expired_access_cookie_goes_to_refresh_first() -> None:
    from radreport.auth.lab import ACCESS_COOKIE

    client = TestClient(create_app(), follow_redirects=False)
    client.cookies.set(ACCESS_COOKIE, "expired-or-garbage")
    assert client.get("/ui/queue").headers["location"].startswith("/ui/refresh?next=")
    assert client.get("/review/queue").status_code == 401, "an API call gets a status code, not a redirect"


def test_a_lab_cookie_authenticates_but_only_same_site_writes(client: TestClient) -> None:
    from radreport.auth.lab import ACCESS_COOKIE

    token = _bearer(RADIOLOGIST, UserRole.RADIOLOGIST)["Authorization"].split(" ", 1)[1]
    client.cookies.set(ACCESS_COOKIE, token)
    assert client.post("/lab/sign", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/lab/sign", headers={"Origin": "https://evil.example"}).status_code == 403


def test_sign_in_never_redirects_off_site() -> None:
    client = TestClient(create_app(), follow_redirects=False)
    for target in ("https://evil.example", "//evil.example", "/admin/labs", "/ui/../admin"):
        assert client.get("/ui/login", params={"next": target}).status_code == 400, target
