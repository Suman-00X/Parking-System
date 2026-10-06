"""The admin panel against a real database: sign-in, per-lab model configuration, platform users and the access policy."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select as sa_select

from radreport.admin import auth
from radreport.admin.modelconfig import ConfigRefused, available_models, create_definition, create_provider, propose_assignment, resolve_api_key, step_configuration, unconfigured_steps
from radreport.api.app import create_app
from radreport.core.types import AssignmentStatus, PlatformRole, ProviderKind, TaskKey, TenantStatus
from radreport.db.models.modelconfig import ModelProvider
from radreport.db.models.tenancy import AdminSession, PlatformUser, Tenant
from radreport.db.session import system_session

pytestmark = pytest.mark.db

PASSWORD = "correct horse battery staple"


@pytest.fixture
def admin_fixture(migrated_db: str, two_tenants):
    """A product admin with a password, and a lab to configure."""
    tenant_id, _other = two_tenants
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
    with system_session(migrated_db) as session:
        session.add(PlatformUser(email=email, display_name="Product Admin", role=PlatformRole.PRODUCT_ADMIN))
        session.flush()
        user = auth.set_password(session, email=email, password=PASSWORD)
        admin_id = user.id
    return {"db": migrated_db, "tenant_id": tenant_id, "email": email, "admin_id": admin_id}


# ========================================================= authentication ===
def test_a_correct_password_opens_a_session(admin_fixture) -> None:
    f = admin_fixture
    with system_session(f["db"]) as session:
        result = auth.login(session, email=f["email"], password=PASSWORD)
        assert result.platform_user_id == f["admin_id"]

        admin = auth.authenticate(session, result.token)
        assert admin is not None
        assert admin.is_product_admin


def test_a_wrong_password_is_refused(admin_fixture) -> None:
    f = admin_fixture
    with system_session(f["db"]) as session:
        with pytest.raises(auth.AuthenticationFailed):
            auth.login(session, email=f["email"], password="wrong password here")


def test_an_unknown_account_fails_the_same_way(admin_fixture) -> None:
    """Distinguishing "no such admin" from "wrong password" turns the login page into an account enumerator."""
    f = admin_fixture
    with system_session(f["db"]) as session:
        with pytest.raises(auth.AuthenticationFailed):
            auth.login(session, email="nobody@example.com", password=PASSWORD)


def test_an_admin_with_no_password_cannot_log_in(migrated_db: str) -> None:
    """A seeded account with a NULL hash must not be treated as "no password required" — that is the direction that fails open."""
    email = f"seeded-{uuid.uuid4().hex[:8]}@example.com"
    with system_session(migrated_db) as session:
        session.add(PlatformUser(email=email, display_name="Seeded", role=PlatformRole.PRODUCT_ADMIN))
        session.flush()
        with pytest.raises(auth.AuthenticationFailed):
            auth.login(session, email=email, password="")
        with pytest.raises(auth.AuthenticationFailed):
            auth.login(session, email=email, password="anything at all here")


def test_only_the_token_hash_is_stored(admin_fixture) -> None:
    """A database read must not hand the reader a working session."""
    f = admin_fixture
    with system_session(f["db"]) as session:
        result = auth.login(session, email=f["email"], password=PASSWORD)
        record = session.get(AdminSession, result.session_id)

        assert record.token_hash != result.token
        assert result.token not in record.token_hash


def test_logout_revokes_immediately(admin_fixture) -> None:
    f = admin_fixture
    with system_session(f["db"]) as session:
        result = auth.login(session, email=f["email"], password=PASSWORD)
        assert auth.authenticate(session, result.token) is not None

        auth.logout(session, token=result.token)
        assert auth.authenticate(session, result.token) is None


def test_an_expired_session_stops_working(admin_fixture) -> None:
    f = admin_fixture
    with system_session(f["db"]) as session:
        result = auth.login(session, email=f["email"], password=PASSWORD)
        record = session.get(AdminSession, result.session_id)
        record.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        session.flush()

        assert auth.authenticate(session, result.token) is None


def test_deactivating_an_admin_kills_their_live_sessions(admin_fixture) -> None:
    """Deactivation must take effect now, not at their next login."""
    f = admin_fixture
    with system_session(f["db"]) as session:
        result = auth.login(session, email=f["email"], password=PASSWORD)
        user = session.get(PlatformUser, f["admin_id"])
        user.is_active = False
        session.flush()

        assert auth.authenticate(session, result.token) is None


def test_changing_a_password_revokes_existing_sessions(admin_fixture) -> None:
    """A password change that leaves old sessions alive does not achieve what the person changing it believes it does."""
    f = admin_fixture
    with system_session(f["db"]) as session:
        result = auth.login(session, email=f["email"], password=PASSWORD)
        auth.set_password(session, email=f["email"], password="a different long one")

        assert auth.authenticate(session, result.token) is None


# ======================================================= model configuration ==
def test_a_cloud_provider_must_name_an_env_var_that_exists(admin_fixture) -> None:
    """Refusing here turns a silent runtime failure into a configuration error the admin sees while configuring."""
    f = admin_fixture
    with system_session(f["db"]) as session:
        with pytest.raises(ConfigRefused) as exc:
            create_provider(session, name="p1", kind=ProviderKind.CLOUD_API, actor_id=f["admin_id"])
        assert exc.value.code == "no_env_var"

        with pytest.raises(ConfigRefused) as exc:
            create_provider(session, name="p2", kind=ProviderKind.CLOUD_API, api_key_env_var="DEFINITELY_NOT_SET_ANYWHERE", actor_id=f["admin_id"])
        assert exc.value.code == "env_var_unset"


def test_the_api_key_never_reaches_the_database(admin_fixture, monkeypatch) -> None:
    """The env var's *name* is stored; the secret stays in the environment."""
    f = admin_fixture
    monkeypatch.setenv("TEST_PROVIDER_KEY", "sk-super-secret-value")
    with system_session(f["db"]) as session:
        provider = create_provider(session, name=f"cloud-{uuid.uuid4().hex[:6]}", kind=ProviderKind.CLOUD_API, api_key_env_var="TEST_PROVIDER_KEY", actor_id=f["admin_id"])
        assert provider.api_key_env_var == "TEST_PROVIDER_KEY"

        # Nothing on the row holds the secret.
        stored = {str(getattr(provider, c.name)) for c in ModelProvider.__table__.c}
        assert not any("sk-super-secret-value" in v for v in stored)

        # And it resolves from the environment at call time.
        assert resolve_api_key(provider) == "sk-super-secret-value"


def test_a_missing_env_var_at_call_time_raises(admin_fixture, monkeypatch) -> None:
    f = admin_fixture
    monkeypatch.setenv("TEMP_KEY", "value")
    with system_session(f["db"]) as session:
        provider = create_provider(session, name=f"c-{uuid.uuid4().hex[:6]}", kind=ProviderKind.CLOUD_API, api_key_env_var="TEMP_KEY", actor_id=f["admin_id"])
        monkeypatch.delenv("TEMP_KEY")
        with pytest.raises(ConfigRefused) as exc:
            resolve_api_key(provider)
        assert exc.value.code == "env_var_unset"


def test_a_local_provider_needs_an_address_not_a_key(admin_fixture) -> None:
    """The path: a lab's own box, no cloud credential involved."""
    f = admin_fixture
    with system_session(f["db"]) as session:
        with pytest.raises(ConfigRefused) as exc:
            create_provider(session, name="local-1", kind=ProviderKind.LOCAL_OPENAI_COMPATIBLE, actor_id=f["admin_id"])
        assert exc.value.code == "no_endpoint"

        provider = create_provider(session, name=f"local-{uuid.uuid4().hex[:6]}", kind=ProviderKind.LOCAL_OPENAI_COMPATIBLE, default_endpoint="http://10.0.0.5:8000/v1", auth_method="none", actor_id=f["admin_id"])
        assert resolve_api_key(provider) is None


def test_a_date_suffixed_identifier_is_refused(admin_fixture, monkeypatch) -> None:
    """Plan: the API rejects them, and the example value is stale."""
    f = admin_fixture
    monkeypatch.setenv("K", "v")
    with system_session(f["db"]) as session:
        provider = create_provider(session, name=f"c-{uuid.uuid4().hex[:6]}", kind=ProviderKind.CLOUD_API, api_key_env_var="K", actor_id=f["admin_id"])
        with pytest.raises(ConfigRefused) as exc:
            create_definition(session, provider_id=provider.id, model_identifier="claude-sonnet-5-20260415", display_name="Sonnet 5", actor_id=f["admin_id"])
        assert exc.value.code == "dated_identifier"


def test_a_consequential_task_refuses_a_local_model(admin_fixture) -> None:
    """The scope discipline: extraction, self-correction, verification and ASR stay on a frontier model regardless of local hardware."""
    f = admin_fixture
    with system_session(f["db"]) as session:
        provider = create_provider(session, name=f"local-{uuid.uuid4().hex[:6]}", kind=ProviderKind.LOCAL_OPENAI_COMPATIBLE, default_endpoint="http://10.0.0.5:8000/v1", auth_method="none", actor_id=f["admin_id"])
        definition = create_definition(session, provider_id=provider.id, model_identifier="qwen3-32b", display_name="Qwen3 32B", actor_id=f["admin_id"])

        with pytest.raises(ConfigRefused) as exc:
            propose_assignment(session, tenant_id=f["tenant_id"], task_key=TaskKey.EXTRACTION, model_definition_id=definition.id, actor_id=f["admin_id"])
        assert exc.value.code == "consequential_task_local_model"

        # A bounded task accepts it.
        assignment = propose_assignment(session, tenant_id=f["tenant_id"], task_key=TaskKey.ROUTING_SHORTLIST, model_definition_id=definition.id, actor_id=f["admin_id"])
        assert assignment.status == AssignmentStatus.PROPOSED


def test_asr_engine_selection_is_a_task_assignment(admin_fixture, monkeypatch) -> None:
    """Choosing an engine goes through the same path — and the same gate — as choosing a model."""
    f = admin_fixture
    monkeypatch.setenv("DG_KEY", "v")
    with system_session(f["db"]) as session:
        provider = create_provider(session, name=f"deepgram-{uuid.uuid4().hex[:6]}", kind=ProviderKind.CLOUD_API, api_key_env_var="DG_KEY", actor_id=f["admin_id"])
        engine = create_definition(session, provider_id=provider.id, model_identifier="nova-3-medical", display_name="Nova-3 Medical", actor_id=f["admin_id"])
        assignment = propose_assignment(session, tenant_id=f["tenant_id"], task_key=TaskKey.ASR_PRIMARY, model_definition_id=engine.id, actor_id=f["admin_id"])

        assert assignment.task_key == TaskKey.ASR_PRIMARY
        # Transcription is upstream of everything, so it is consequential.
        assert assignment.task_bucket == "consequential"
        # Proposed, not active: needs a gold-set eval run first.
        assert assignment.status == AssignmentStatus.PROPOSED


def test_another_labs_private_model_is_not_offered(admin_fixture, two_tenants) -> None:
    """A dropdown listing every lab's local endpoints would be a directory of other customers' infrastructure."""
    f = admin_fixture
    _mine, theirs = two_tenants
    with system_session(f["db"]) as session:
        provider = create_provider(session, name=f"their-box-{uuid.uuid4().hex[:6]}", kind=ProviderKind.LOCAL_OPENAI_COMPATIBLE, default_endpoint="http://192.168.9.9:8000/v1", auth_method="none", tenant_id=theirs, actor_id=f["admin_id"])
        create_definition(session, provider_id=provider.id, model_identifier="their-model", display_name="Their Model", tenant_id=theirs, actor_id=f["admin_id"])

        offered = available_models(session, tenant_id=f["tenant_id"])
        assert all(d.display_name != "Their Model" for d, _p in offered)


def test_every_step_is_listed_including_the_unconfigured_ones(admin_fixture) -> None:
    """The admin panel's job is to show what is missing as much as what is set."""
    f = admin_fixture
    with system_session(f["db"]) as session:
        steps = step_configuration(session, tenant_id=f["tenant_id"])

        assert {s.task_key for s in steps} == set(TaskKey.values())
        assert all(not s.is_configured for s in steps)
        assert TaskKey.ASR_PRIMARY in unconfigured_steps(session, tenant_id=f["tenant_id"])
        assert any(s.is_asr for s in steps)


# ============================================================ admin panel ===
def _signed_in(email: str) -> tuple[TestClient, str]:
    client = TestClient(create_app(), follow_redirects=False)
    cookie = client.post("/admin/login", data={"email": email, "password": PASSWORD}).cookies.get(auth.SESSION_COOKIE)
    assert cookie
    client.cookies.set(auth.SESSION_COOKIE, cookie)
    return client, cookie


def _support_account(db: str) -> str:
    email = f"support-{uuid.uuid4().hex[:8]}@example.com"
    with system_session(db) as session:
        session.add(PlatformUser(email=email, display_name="Support", role=PlatformRole.SUPPORT))
        session.flush()
        auth.set_password(session, email=email, password=PASSWORD)
    return email


def test_the_admin_panel_requires_a_session(admin_fixture) -> None:
    """The header that used to grant admin reaches nothing."""
    client = TestClient(create_app(), follow_redirects=False)
    for path in ("/admin/labs", "/admin/providers", "/admin/users"):
        assert client.get(path).headers["location"] == "/admin/login"
        assert client.get(path, headers={"X-Platform-User-Id": str(admin_fixture["admin_id"])}).headers["location"] == "/admin/login"
    assert client.get("/admin/api/labs", headers={"X-Platform-User-Id": str(admin_fixture["admin_id"])}).status_code == 401


def test_signing_in_through_the_admin_panel_works(admin_fixture) -> None:
    f = admin_fixture
    client = TestClient(create_app(), follow_redirects=False)

    bad = client.post("/admin/login", data={"email": f["email"], "password": "wrong one here"})
    assert bad.status_code == 303
    assert "error=" in bad.headers["location"]

    good = client.post("/admin/login", data={"email": f["email"], "password": PASSWORD})
    assert good.status_code == 303
    assert good.headers["location"] == "/admin/labs"

    cookie = good.cookies.get(auth.SESSION_COOKIE)
    assert cookie
    page = client.get("/admin/labs", cookies={auth.SESSION_COOKIE: cookie})
    assert page.status_code == 200
    assert "Onboard a lab" in page.text


def test_support_can_read_but_not_change_anything(admin_fixture) -> None:
    """Support gets visibility, not configuration."""
    f = admin_fixture
    client, _cookie = _signed_in(_support_account(f["db"]))

    page = client.get("/admin/labs")
    assert page.status_code == 200
    assert "Onboard a lab" not in page.text, "controls support cannot use are hidden"
    assert client.get(f"/admin/labs/{f['tenant_id']}/readiness").status_code == 200
    assert client.get("/admin/api/labs").status_code == 200

    assert client.post("/admin/labs", data={"name": "x", "slug": "xx", "admin_display_name": "a", "admin_email": "a@b.c", "admin_employee_code": "1"}).status_code == 403
    assert client.post("/admin/api/users", json={"email": "e@x.com", "display_name": "E", "role": "support", "password": PASSWORD}).status_code == 403
    assert client.post(f"/admin/api/labs/{f['tenant_id']}/status", json={"status": "pilot"}).status_code == 403


def test_a_bad_slug_is_refused_by_the_server_not_only_the_form(admin_fixture) -> None:
    """The access policy's pattern rejects it before the handler runs, on the page and the API alike."""
    client, _cookie = _signed_in(admin_fixture["email"])
    page = client.post("/admin/labs", data={"name": "Bad", "slug": "Not A Slug!", "admin_display_name": "a", "admin_email": "a@b.c", "admin_employee_code": "1"})
    assert page.status_code == 400
    assert "'slug'" in page.json()["detail"]
    assert client.post("/admin/api/labs", json={"name": "Bad", "slug": "Not A Slug!", "admin_email": "a@b.c", "admin_display_name": "a", "admin_employee_code": "1"}).status_code == 400


def test_an_extra_field_cannot_be_smuggled_into_registration(admin_fixture) -> None:
    """Pydantic would silently drop an unknown key; the policy refuses it outright."""
    client, _cookie = _signed_in(admin_fixture["email"])
    body = {"name": "Lab", "slug": f"lab-{uuid.uuid4().hex[:6]}", "admin_email": "a@b.c", "admin_display_name": "a", "admin_employee_code": "1", "status": "live"}
    response = client.post("/admin/api/labs", json=body)
    assert response.status_code == 400
    assert "'status' is not accepted" in response.json()["detail"]


def test_pooling_consent_needs_its_contract_reference(admin_fixture) -> None:
    client, _cookie = _signed_in(admin_fixture["email"])
    response = client.post("/admin/labs", data={"name": "Lab", "slug": f"lab-{uuid.uuid4().hex[:6]}", "admin_display_name": "a", "admin_email": "a@b.c", "admin_employee_code": "1", "training_pooling_consent": "1"})
    assert "contract+reference" in response.headers["location"]


def test_a_proposed_assignment_reads_back(admin_fixture, monkeypatch) -> None:
    """Write then read, which the empty-case test above could not check."""
    f = admin_fixture
    monkeypatch.setenv("RB_KEY", "v")
    with system_session(f["db"]) as session:
        provider = create_provider(session, name=f"cloud-{uuid.uuid4().hex[:6]}", kind=ProviderKind.CLOUD_API, api_key_env_var="RB_KEY", actor_id=f["admin_id"])
        definition = create_definition(session, provider_id=provider.id, model_identifier="claude-sonnet-5", display_name="Sonnet 5", actor_id=f["admin_id"])
        propose_assignment(session, tenant_id=f["tenant_id"], task_key=TaskKey.EXTRACTION, model_definition_id=definition.id, actor_id=f["admin_id"])

    # A fresh session, as a later request would have.
    with system_session(f["db"]) as session:
        steps = {s.task_key: s for s in step_configuration(session, tenant_id=f["tenant_id"])}
        extraction = steps[TaskKey.EXTRACTION]

        assert extraction.proposed, "the proposal must be visible on the next request"
        assert "Sonnet 5" in extraction.proposed[0][1]
        # Proposed is not active: still needs the eval run.
        assert extraction.is_configured is False


def test_a_local_provider_from_the_form_is_assignable(admin_fixture) -> None:
    """`auth_method` defaults to `api_key` at the column, and the admin panel form does not send one."""
    f = admin_fixture
    with system_session(f["db"]) as session:
        provider = create_provider(
            session,
            name=f"box-{uuid.uuid4().hex[:6]}",
            kind=ProviderKind.LOCAL_OPENAI_COMPATIBLE,
            default_endpoint="http://10.0.0.5:8000/v1",
            actor_id=f["admin_id"],
            # No auth_method and no env var, exactly as the form posts it.
        )
        assert resolve_api_key(provider) is None

        definition = create_definition(session, provider_id=provider.id, model_identifier="qwen3-32b", display_name="Qwen3 32B", actor_id=f["admin_id"])
        assignment = propose_assignment(session, tenant_id=f["tenant_id"], task_key=TaskKey.ROUTING_SHORTLIST, model_definition_id=definition.id, actor_id=f["admin_id"])
        assert assignment.status == AssignmentStatus.PROPOSED


# ============================================================== readiness ===
def test_the_readiness_screen_lists_every_check(admin_fixture) -> None:
    f = admin_fixture
    client, _cookie = _signed_in(f["email"])

    page = client.get(f"/admin/labs/{f['tenant_id']}/readiness")
    assert page.status_code == 200
    # Passing and failing alike, so the screen is a report rather than a verdict.
    for check_id in ("collision_audit_clear", "corpus_template_coverage", "voice_enrollment_complete"):
        assert check_id in page.text
    # A lab with no onboarding data behind it cannot be ready.
    assert "blocking" in page.text


def test_the_readiness_screen_sees_the_labs_own_rows(admin_fixture) -> None:
    """The bug this closes: the screen ran with no lab bound, so RLS hid every row and every check failed."""
    from radreport.db.models.evaluation import EvalSet
    from radreport.db.session import tenant_session

    f = admin_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        eval_set = EvalSet(tenant_id=f["tenant_id"], name=f"acc-{uuid.uuid4().hex[:6]}", is_frozen=True, is_canonical=False)
        session.add(eval_set)
        session.flush()
        eval_set_id = str(eval_set.id)

    client, _cookie = _signed_in(f["email"])
    report = client.get(f"/admin/api/labs/{f['tenant_id']}/readiness").json()
    gold = next(c for c in report["checks"] if c["check_id"] == "gold_set_frozen")
    # Still failing on size (no items), but the check found the lab's frozen set rather than "none exists".
    assert eval_set_id in gold["detail"]["item_counts"], gold


def test_the_readiness_screen_does_not_record_a_check(admin_fixture) -> None:
    """Opening a screen must not write history."""
    from radreport.db.models.onboarding import OnboardingReadinessCheck

    f = admin_fixture
    client, _cookie = _signed_in(f["email"])
    client.get(f"/admin/labs/{f['tenant_id']}/readiness")

    with system_session(f["db"]) as session:
        rows = session.query(OnboardingReadinessCheck).filter_by(tenant_id=f["tenant_id"]).count()
    assert rows == 0


def test_an_unknown_lab_has_no_readiness_screen(admin_fixture) -> None:
    client, _cookie = _signed_in(admin_fixture["email"])
    assert client.get(f"/admin/labs/{uuid.uuid4()}/readiness").status_code == 404


def test_the_lab_page_offers_status_moves_and_links_to_readiness(admin_fixture) -> None:
    f = admin_fixture
    with system_session(f["db"]) as session:
        tenant = session.get(Tenant, f["tenant_id"])
        assert tenant is not None
        tenant.status = TenantStatus.ONBOARDING

    client, _cookie = _signed_in(f["email"])
    page = client.get(f"/admin/labs/{f['tenant_id']}")
    assert page.status_code == 200
    assert f"/admin/labs/{f['tenant_id']}/readiness" in page.text
    assert f"/admin/labs/{f['tenant_id']}/status" in page.text
    assert client.get(f"/admin/tenants/{f['tenant_id']}/readiness").status_code == 404


def test_the_pilot_move_is_refused_until_readiness_passes(admin_fixture) -> None:
    f = admin_fixture
    with system_session(f["db"]) as session:
        session.get(Tenant, f["tenant_id"]).status = TenantStatus.ONBOARDING

    client, _cookie = _signed_in(f["email"])
    assert client.post(f"/admin/api/labs/{f['tenant_id']}/status", json={"status": "pilot"}).status_code == 409
    page = client.post(f"/admin/labs/{f['tenant_id']}/status", data={"status": "pilot"})
    assert "error=" in page.headers["location"]


# ========================================================= platform users ===
def test_a_product_admin_adds_a_support_account_that_can_sign_in(admin_fixture) -> None:
    client, _cookie = _signed_in(admin_fixture["email"])
    email = f"new-{uuid.uuid4().hex[:8]}@example.com"
    created = client.post("/admin/api/users", json={"email": email, "display_name": "New", "role": "support", "password": PASSWORD})
    assert created.status_code == 201
    assert created.json()["role"] == "support"

    assert client.post("/admin/api/users", json={"email": email, "display_name": "Dup", "role": "support", "password": PASSWORD}).status_code == 409
    assert client.post("/admin/api/users", json={"email": f"x{email}", "display_name": "Short", "role": "support", "password": "short"}).status_code == 422

    other = TestClient(create_app(), follow_redirects=False)
    assert other.post("/admin/login", data={"email": email, "password": PASSWORD}).headers["location"] == "/admin/labs"


def test_deactivation_ends_sessions_and_spares_the_last_admin(admin_fixture) -> None:
    f = admin_fixture
    client, _cookie = _signed_in(f["email"])
    support_email = _support_account(f["db"])
    support_client, _ = _signed_in(support_email)
    assert support_client.get("/admin/labs").status_code == 200

    with system_session(f["db"]) as session:
        support_id = session.execute(sa_select(PlatformUser.id).where(PlatformUser.email == support_email)).scalar_one()
    assert client.post(f"/admin/api/users/{support_id}/deactivate").json()["is_active"] is False
    assert support_client.get("/admin/labs").headers["location"] == "/admin/login"

    assert client.post(f"/admin/api/users/{f['admin_id']}/deactivate").status_code == 422, "you cannot deactivate yourself"


def test_platform_user_changes_are_audited(admin_fixture) -> None:
    from radreport.db.models.orchestration import AuditLog

    client, _cookie = _signed_in(admin_fixture["email"])
    email = f"aud-{uuid.uuid4().hex[:8]}@example.com"
    user_id = client.post("/admin/api/users", json={"email": email, "display_name": "A", "role": "support", "password": PASSWORD}).json()["id"]
    with system_session(admin_fixture["db"]) as session:
        actions = {a.action for a in session.query(AuditLog).filter_by(entity_id=uuid.UUID(user_id)).all()}
    assert "platform_user_created" in actions


# ============================================================ lab side ===
def test_a_lab_route_refuses_a_lab_user_without_the_role(admin_fixture) -> None:
    """The access policy checks the stored roles before the handler runs."""
    from radreport.core.types import UserRole
    from radreport.db.models.identity import AppUser
    from radreport.db.session import tenant_session

    f = admin_fixture
    with tenant_session(f["tenant_id"], url=f["db"]) as session:
        auditor = AppUser(tenant_id=f["tenant_id"], employee_code=f"A-{uuid.uuid4().hex[:6]}", display_name="Auditor", roles=[UserRole.AUDITOR])
        session.add(auditor)
        session.flush()
        auditor_id = auditor.id

    client = TestClient(create_app(), follow_redirects=False)
    headers = {"X-User-Id": str(auditor_id), "X-Tenant-Id": str(f["tenant_id"])}
    assert client.post(f"/onboarding/critical-rules/{uuid.uuid4()}/approve", headers=headers).status_code == 403
    assert client.get("/review/queue", headers=headers).status_code == 200
