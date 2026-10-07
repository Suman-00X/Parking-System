"""The admin panel's pages, rendered on the server.

Order: sign in (login_page, login_submit, logout_submit) -> manage labs (home, labs_page,
create_lab, lab_page, set_lab_user_password, change_status, lab_readiness_page) -> configure models per step
(assign_step, activate_step, providers_page, add_provider, add_model) -> onboard a lab
(onboarding_page, upload_roster, upload_templates, upload_shorthand, upload_corpus, merge_proposals, run_onboarding_step) ->
manage platform users (users_page, create_user, deactivate_user, reactivate_user, reset_password)
-> your own account (account_page, change_own_password).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from typing import Annotated
from urllib.parse import urlencode

import sqlalchemy as sa
from fastapi import APIRouter, Cookie, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from radreport.adapters.llm.registry import activate_assignment
from radreport.admin import auth, onboarding_steps, users
from radreport.admin.auth import AuthenticatedAdmin, AuthenticationFailed
from radreport.admin.modelconfig import ConfigRefused, available_models, create_definition, create_provider, propose_assignment, step_configuration
from radreport.admin.onboarding_steps import StepRefused
from radreport.api.access import load_policy
from radreport.api.deps import CurrentAdmin, admin_lab_session, client_ip
from radreport.api.pagination import Page, paginate
from radreport.api.routes.admin_api import SLUG_PATTERN, lab_user_out
from radreport.api.ui import admin_page, auth_page, badge, banner, card, esc, facts, flash, icon, pager, progress, stat, status_badge, steps, table
from radreport.auth import lab as lab_auth
from radreport.core.config import get_settings
from radreport.core.errors import ModelResolutionError, UngatedActivation
from radreport.core.tenancy import TenantTransitionError, allowed_transitions
from radreport.core.types import CheckStatus, ImportBatchType, PlatformRole, ProviderKind, TenantStatus
from radreport.db.models.identity import AppUser
from radreport.db.models.modelconfig import ModelDefinition, ModelProvider
from radreport.db.models.tenancy import PlatformUser, Tenant
from radreport.db.session import read_session, system_session
from radreport.onboarding.batches import ArtifactUpload
from radreport.onboarding.readiness import evaluate_readiness
from radreport.onboarding.registration import LabRegistration, register_lab, transition_status

router = APIRouter(prefix="/admin", tags=["admin-panel"])

_esc = esc

#: The lifecycle a lab moves through, shown as a progress strip on its page.
_LIFECYCLE: tuple[tuple[str, str], ...] = ((TenantStatus.PROVISIONING, "Registered"), (TenantStatus.ONBOARDING, "Data and approvals"), (TenantStatus.PILOT, "Shadow and first reports"), (TenantStatus.LIVE, "In clinical use"))


def _redirect(path: str, *, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    """Back to a page, with a message carried in a properly encoded query string."""
    query = urlencode({k: v for k, v in (("error", error), ("notice", notice)) if v})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=status.HTTP_303_SEE_OTHER)


def _can(admin: AuthenticatedAdmin, method: str, path: str) -> bool:
    """Whether the access policy lets this admin call a route; hides controls they cannot use."""
    return load_policy().allows(admin.role, method, path)


def _number(value: float | None) -> str:
    """Counts render as counts, rates as rates."""
    if value is None:
        return "&mdash;"
    return str(int(value)) if value == int(value) else f"{value:.3f}"


def _detail(detail: dict[str, object] | None) -> str:
    if not detail:
        return ""
    return f'<div class="meta">{_esc(", ".join(f"{k}: {v}" for k, v in detail.items()))}</div>'


def _messages(error: str | None, notice: str | None) -> str:
    return flash(error, notice)


def _page(title: str, body: str, *, admin: AuthenticatedAdmin, active: str = "", subtitle: str | None = None, eyebrow: str | None = None, actions: str = "", crumbs: Sequence[tuple[str, str | None]] = ()) -> HTMLResponse:
    return admin_page(title, body, admin_name=admin.display_name, admin_role=admin.role, active=active, subtitle=subtitle, eyebrow=eyebrow, actions=actions, crumbs=crumbs)


def _lab_crumbs(tenant_id: uuid.UUID, name: str, *tail: str) -> tuple[tuple[str, str | None], ...]:
    return (("Labs", "/admin/labs"), (name, f"/admin/labs/{tenant_id}" if tail else None), *((t, None) for t in tail))


# =================================================================== login ===
def _demo_credentials_panel() -> str:
    """The test-credentials tab body, one card per configured demo account."""
    cards = "".join(
        f"""<div class="cred"><div class="label">{_esc(a.label)} {badge(a.role, "brand")}</div>
      <div>Email: <code>{_esc(a.email)}</code></div><div style="margin-top:4px">Password: <code>{_esc(a.password)}</code></div>
      <div class="actions" style="margin-top:10px"><button type="button" class="sm" data-fill-email="{_esc(a.email)}" data-fill-password="{_esc(a.password)}">{icon("arrow")}Use these</button></div></div>"""
        for a in get_settings().demo_accounts
    )
    return f'<p class="meta">Demo accounts for trying the product. They hold synthetic data only.</p>{cards}'


@router.get("/login", response_class=HTMLResponse)
def login_page(error: str | None = None) -> HTMLResponse:
    form = """<form method="post" action="/admin/login">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" required autocomplete="username" placeholder="you@company.com">
      <label for="password">Password</label>
      <input id="password" name="password" type="password" required autocomplete="current-password" placeholder="••••••••••••">
      <div class="actions"><button class="primary" type="submit">Sign in</button></div>
    </form>"""
    subtitle = "Product admins and support sign in here to configure labs, models and accounts."
    if not get_settings().demo_accounts:
        return auth_page("Sign in", f"{_messages(error, None)}{form}", subtitle=subtitle, realm="Admin panel")
    return auth_page(
        "Sign in",
        f"""{_messages(error, None)}
    <div class="tabs" role="tablist" data-tabs>
      <button type="button" role="tab" aria-selected="true" data-panel="panel-signin">Sign in</button>
      <button type="button" role="tab" aria-selected="false" data-panel="panel-demo">Test credentials</button>
    </div>
    <div id="panel-signin" role="tabpanel">{form}</div>
    <div id="panel-demo" role="tabpanel" hidden>{_demo_credentials_panel()}</div>""",
        subtitle=subtitle,
        realm="Admin panel",
    )


@router.post("/login")
def login_submit(request: Request, email: Annotated[str, Form()], password: Annotated[str, Form()]) -> Response:
    """Authenticate and set the session cookie."""
    with system_session() as session:
        try:
            result = auth.login(session, email=email, password=password, user_agent=request.headers.get("user-agent"), ip_address=client_ip(request))
        except AuthenticationFailed:
            # One message for unknown account and wrong password alike, so the form cannot enumerate accounts.
            return _redirect("/admin/login", error="Invalid email or password")

    response = _redirect("/admin/labs")
    response.set_cookie(
        auth.SESSION_COOKIE,
        result.token,
        httponly=True,
        samesite="lax",
        # Secure outside local development: this cookie carries admin powers.
        secure=get_settings().environment not in ("local", "test", "development"),
        max_age=int(auth.SESSION_TTL.total_seconds()),
        path="/",
    )
    return response


@router.post("/logout")
def logout_submit(radreport_admin: Annotated[str | None, Cookie()] = None) -> Response:
    with system_session() as session:
        auth.logout(session, token=radreport_admin)
    response = _redirect("/admin/login")
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


# ==================================================================== labs ===
@router.get("")
def home(admin: CurrentAdmin) -> Response:
    return _redirect("/admin/labs")


@router.get("/labs", response_class=HTMLResponse)
def labs_page(admin: CurrentAdmin, error: str | None = None, notice: str | None = None, show: str | None = None, page: int | None = None) -> HTMLResponse:
    """The lab list, a page at a time; offboarded labs are hidden unless asked for."""
    show_all = show == "all"
    with read_session() as session:
        counts = dict(session.execute(sa.select(Tenant.status, sa.func.count()).group_by(Tenant.status)).all())
        query = sa.select(Tenant).order_by(Tenant.name, Tenant.id)
        if not show_all:
            query = query.where(Tenant.status != TenantStatus.OFFBOARDED)
        paged = paginate(session, query, Page.of(page, 25))
        tenants = paged.rows
        rows = [
            f"""<tr>
              <td><div class="row" style="gap:12px;flex-wrap:nowrap"><span class="avatar" style="border-radius:10px">{_esc(t.name[:2].upper())}</span><div><strong><a href="/admin/labs/{t.id}">{_esc(t.name)}</a></strong><div class="cell-sub mono">{_esc(t.slug)}</div></div></div></td>
              <td>{status_badge(t.status)}</td>
              <td>{badge("pooling consented", "ok") if t.training_pooling_consent else badge("no pooling", "muted")}</td>
              <td class="num"><a class="btn sm ghost" href="/admin/labs/{t.id}">Open {icon("arrow")}</a></td>
            </tr>"""
            for t in tenants
        ]

    total = sum(counts.values())
    live = counts.get(TenantStatus.LIVE, 0) + counts.get(TenantStatus.PILOT, 0)
    onboarding = counts.get(TenantStatus.ONBOARDING, 0) + counts.get(TenantStatus.PROVISIONING, 0)
    kpis = f"""<div class="grid cols-4" style="margin-bottom:18px">
      {stat("Labs", str(total), hint="registered on the platform", icon_name="labs")}
      {stat("Live", str(live), hint="in pilot or clinical use", tone="ok", icon_name="check")}
      {stat("Onboarding", str(onboarding), hint="getting data and approvals in", icon_name="onboarding")}
      {stat("Suspended", str(counts.get(TenantStatus.SUSPENDED, 0)), hint="routing to manual fallback", tone="warn" if counts.get(TenantStatus.SUSPENDED) else "", icon_name="alert")}
    </div>"""
    toggle = '<a class="btn sm ghost" href="/admin/labs">Hide offboarded</a>' if show_all else '<a class="btn sm ghost" href="/admin/labs?show=all">Show offboarded</a>'
    lab_table = card(table(("Lab", "Status", "Training data", ""), rows, empty_text="No labs yet. Register the first one below.", empty_icon="labs", numeric=(3,)) + pager(page=paged.page.number, pages=paged.pages, total=paged.total, path="/admin/labs", extra={"show": "all"} if show_all else None), title="All labs", subtitle=f"{paged.total} in all", icon_name="labs", actions=toggle, cls="flush")
    register = (
        card(
            f"""<form method="post" action="/admin/labs">
 <div class="form-grid">
 <div><label for="name">Lab name</label><input id="name" name="name" required placeholder="Sunrise Imaging"></div>
 <div><label for="slug">Slug</label><input id="slug" name="slug" pattern="{_esc(SLUG_PATTERN.strip("^$"))}" required placeholder="sunrise"><div class="hint">Lowercase; the lab's sign-in key and URL.</div></div>
 <div><label for="admin_display_name">Lab administrator</label><input id="admin_display_name" name="admin_display_name" required></div>
 <div><label for="admin_email">Their email</label><input id="admin_email" name="admin_email" type="email" required></div>
 <div><label for="admin_employee_code">Their employee code</label><input id="admin_employee_code" name="admin_employee_code" required></div>
 <div><label for="patient_notice_version">Patient-notice wording version</label><input id="patient_notice_version" name="patient_notice_version"></div>
 <div class="span-all"><label class="check"><input type="checkbox" name="training_pooling_consent" value="1"> <span>The signed contract includes the cross-tenant training pooling clause</span></label></div>
 <div class="span-all"><label for="training_consent_ref">Contract reference for that clause</label><input id="training_consent_ref" name="training_consent_ref" placeholder="required if the box is ticked"></div>
 </div>
 <p class="hint">Pooling consent is captured here because registration is the one moment the answer is actually known; it is near-impossible to retrofit later.</p>
 <div class="actions"><button class="primary" type="submit">{icon("plus")}Register</button></div>
 </form>""",
            title="Onboard a lab",
            subtitle="Creates the lab and its first lab administrator.",
            icon_name="plus",
            id="register",
        )
        if _can(admin, "POST", "/admin/labs")
        else ""
    )
    actions = f'<a class="btn primary" href="#register">{icon("plus")}Onboard a lab</a>' if register else ""
    return _page("Labs", f"""{_messages(error, notice)}{kpis}{lab_table}<div class="section">{register}</div>""", admin=admin, active="labs", subtitle="Every lab on the platform, where it is in its lifecycle, and whether it has agreed to pool training data.", eyebrow="Workspace", actions=actions)


@router.post("/labs")
def create_lab(admin: CurrentAdmin, name: Annotated[str, Form()], slug: Annotated[str, Form()], admin_display_name: Annotated[str, Form()], admin_email: Annotated[str, Form()], admin_employee_code: Annotated[str, Form()], training_pooling_consent: Annotated[str | None, Form()] = None, training_consent_ref: Annotated[str | None, Form()] = None, patient_notice_version: Annotated[str | None, Form()] = None) -> Response:
    if not re.fullmatch(SLUG_PATTERN, slug):
        return _redirect("/admin/labs", error="slug must be 2-63 lowercase letters, digits or hyphens, starting with a letter or digit")
    consent = bool(training_pooling_consent)
    if consent and not (training_consent_ref or "").strip():
        return _redirect("/admin/labs", error="pooling consent needs the contract reference it comes from")
    with system_session() as session:
        try:
            result = register_lab(session, LabRegistration(name=name, slug=slug, admin_email=admin_email, admin_display_name=admin_display_name, admin_employee_code=admin_employee_code, training_pooling_consent=consent, training_consent_ref=(training_consent_ref or "").strip() or None, patient_notice_version=(patient_notice_version or "").strip() or None), actor_id=admin.platform_user_id)
        except ValueError as exc:
            return _redirect("/admin/labs", error=str(exc))
        tenant_id = result.tenant.id
    return _redirect(f"/admin/labs/{tenant_id}", notice="Lab registered")


def _lifecycle(current: str) -> str:
    order = [s for s, _ in _LIFECYCLE]
    if current in order:
        return steps([(s.title(), d) for s, d in _LIFECYCLE], current=order.index(current))
    return banner(f"This lab is <strong>{_esc(current)}</strong>.", tone="warn" if current == TenantStatus.SUSPENDED else "info")


@router.get("/labs/{tenant_id}", response_class=HTMLResponse)
def lab_page(tenant_id: uuid.UUID, request: Request, admin: CurrentAdmin, error: str | None = None, notice: str | None = None) -> HTMLResponse:
    """One lab: its status, its pipeline steps, and what serves each of them."""
    can_assign = _can(admin, "POST", f"/admin/labs/{tenant_id}/assign")
    can_activate = _can(admin, "POST", f"/admin/labs/{tenant_id}/assignments/{uuid.UUID(int=0)}/activate")
    can_change_status = _can(admin, "POST", f"/admin/labs/{tenant_id}/status")
    with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
        tenant = session.get(Tenant, tenant_id)
        assert tenant is not None
        steps_config = step_configuration(session, tenant_id=tenant_id)
        models = available_models(session, tenant_id=tenant_id)
        options = "".join(f'<option value="{d.id}">{_esc(d.display_name)} — {_esc(p.name)}{" (local)" if p.kind == ProviderKind.LOCAL_OPENAI_COMPATIBLE else ""}</option>' for d, p in models)

        rows = []
        for step in steps_config:
            active = f"<strong>{_esc(step.active_model)}</strong><div class='cell-sub'>{_esc(step.active_provider)}</div>" if step.is_configured else badge("not configured", "warn")
            pending = "".join(f"<div class='cell-sub'>proposed: {_esc(label)}" + (f" <form class='inline' method='post' action='/admin/labs/{tenant_id}/assignments/{aid}/activate'><button type='submit' class='linkish'>activate</button></form>" if can_activate else "") + "</div>" for aid, label in step.proposed)
            bucket = badge("consequential", "danger") if step.task_bucket == "consequential" else badge("bounded", "muted")
            kind = "ASR engine" if step.is_asr else "Language model"
            change = (
                f"""<form class="inline" method="post" action="/admin/labs/{tenant_id}/assign">
                      <input type="hidden" name="task_key" value="{_esc(step.task_key)}">
                      <select name="model_definition_id" required aria-label="Model for {_esc(step.task_key)}">
                        <option value="">choose a model…</option>{options}
                      </select>
                      <button type="submit" class="sm">Propose</button>
                    </form>"""
                if can_assign
                else ""
            )
            rows.append(f"<tr><td><strong class='mono'>{_esc(step.task_key)}</strong><div class='cell-sub'>{kind}</div></td><td>{bucket}</td><td>{active}{pending}</td><td>{change}</td></tr>")

        targets = sorted(allowed_transitions(tenant.status))
        name, slug, current, consent = tenant.name, tenant.slug, tenant.status, tenant.training_pooling_consent
        staff = [lab_user_out(u) for u in session.execute(sa.select(AppUser).where(AppUser.tenant_id == tenant_id).order_by(AppUser.display_name)).scalars().all()]

    can_set_password = _can(admin, "POST", f"/admin/labs/{tenant_id}/users/{uuid.UUID(int=0)}/password")
    staff_rows = [
        f"""<tr><td><div class="row" style="gap:10px;flex-wrap:nowrap"><span class="avatar">{_esc("".join(w[0] for w in u.display_name.split()[:2]).upper() or "?")}</span><div><strong>{_esc(u.display_name)}</strong><div class="cell-sub">{_esc(u.email or "no email")} · {_esc(u.employee_code)}</div></div></div></td>
            <td>{" ".join(badge(r.replace("_", " "), "brand", dot=False) for r in u.roles)}</td>
            <td>{badge("can sign in", "ok") if u.can_sign_in else badge("cannot sign in", "warn")}</td>
            <td class="nowrap">{_esc(u.last_login_at[:16].replace("T", " ") if u.last_login_at else "never")}</td>
            <td>{f'<form class="inline" method="post" action="/admin/labs/{tenant_id}/users/{u.id}/password"><input name="password" type="password" minlength="{auth.MIN_PASSWORD_LENGTH}" placeholder="new password" required autocomplete="new-password" aria-label="New password for {_esc(u.display_name)}"><button type="submit" class="sm">Set password</button></form>' if can_set_password and u.email else ""}</td></tr>"""
        for u in staff
    ]

    unconfigured = sum(1 for s in steps_config if not s.is_configured)
    warning = banner(f"<strong>{unconfigured} step(s) have no active model.</strong> A pipeline run will fail at the first of them.", tone="blocked") if unconfigured else ""
    status_form = (
        f"""<form class="inline" method="post" action="/admin/labs/{tenant_id}/status">
 <select name="status" required aria-label="Move to status">{"".join(f'<option value="{_esc(t)}">{_esc(t)}</option>' for t in targets)}</select>
 <button type="submit" class="sm">Move</button></form>"""
        if can_change_status and targets
        else ""
    )
    configured = len(steps_config) - unconfigured
    signins = sum(1 for u in staff if u.can_sign_in)
    kpis = f"""<div class="grid cols-4">
      {stat("Status", status_badge(current), hint=f"slug <code>{_esc(slug)}</code>", icon_name="labs")}
      {stat("Pipeline steps", f"{configured}/{len(steps_config)}", hint="have an active model", tone="ok" if not unconfigured else "warn", icon_name="providers")}
      {stat("Staff", str(len(staff)), hint=f"{signins} can sign in", icon_name="users")}
      {stat("Training pooling", "Yes" if consent else "No", hint="from the signed contract", tone="ok" if consent else "", icon_name="shield")}
    </div>"""
    lifecycle = card(f"""{_lifecycle(current)}<div class="spread" style="margin-top:16px"><div class="row"><a class="btn sm" href="/admin/labs/{tenant_id}/onboarding">{icon("onboarding")}Onboarding</a><a class="btn sm" href="/admin/labs/{tenant_id}/readiness">{icon("readiness")}Readiness</a><span class="meta">Readiness gates the move from onboarding to pilot.</span></div>{status_form}</div>""", title="Lifecycle", icon_name="ops")
    pipeline = card(table(("Pipeline step", "Bucket", "Serving", "Change"), rows) + "", title="Models per pipeline step", subtitle="A proposal does not go live. Activation requires a gold-set <code>eval_run</code> for that model; without one it is refused. Cloud API keys are read from the server environment and never entered here.", icon_name="providers", cls="flush")
    staff_card = card(table(("Name", "Roles", "Sign-in", "Last sign-in", ""), staff_rows, empty_text="No staff yet; import a roster from the onboarding page.", empty_icon="users"), title="Lab staff", subtitle=f"Lab staff sign in with this lab's slug (<code>{_esc(slug)}</code>), their email and password. Setting a password signs that person out everywhere.", icon_name="users", cls="flush")
    return _page(name, f"""{_messages(error, notice)}{warning}{kpis}<div class="section">{lifecycle}</div><div class="section">{pipeline}</div><div class="section">{staff_card}</div>""", admin=admin, active="labs", eyebrow="Lab", crumbs=_lab_crumbs(tenant_id, name))


@router.post("/labs/{tenant_id}/users/{user_id}/password")
def set_lab_user_password(tenant_id: uuid.UUID, user_id: uuid.UUID, request: Request, admin: CurrentAdmin, password: Annotated[str, Form()]) -> Response:
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        return _redirect(f"/admin/labs/{tenant_id}", error=f"a password must be at least {auth.MIN_PASSWORD_LENGTH} characters")
    try:
        with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
            user = session.get(AppUser, user_id)
            if user is None or user.tenant_id != tenant_id:
                return _redirect(f"/admin/labs/{tenant_id}", error="no such lab user")
            lab_auth.set_password(session, user_id=user_id, password=password, actor_id=admin.platform_user_id, actor_is_platform=True)
            who = user.display_name
    except ValueError as exc:
        return _redirect(f"/admin/labs/{tenant_id}", error=str(exc))
    return _redirect(f"/admin/labs/{tenant_id}", notice=f"Password set for {who}; their sessions were ended")


@router.post("/labs/{tenant_id}/status")
def change_status(tenant_id: uuid.UUID, request: Request, admin: CurrentAdmin, status_value: Annotated[str, Form(alias="status")]) -> Response:
    try:
        with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
            transition_status(session, tenant_id, status_value, actor_id=admin.platform_user_id)
    except (TenantTransitionError, ValueError) as exc:
        return _redirect(f"/admin/labs/{tenant_id}", error=str(exc))
    return _redirect(f"/admin/labs/{tenant_id}", notice=f"Moved to {status_value}")


@router.get("/labs/{tenant_id}/readiness", response_class=HTMLResponse)
def lab_readiness_page(tenant_id: uuid.UUID, request: Request, admin: CurrentAdmin) -> HTMLResponse:
    """The readiness checks gating this lab's move to pilot. Read-only; nothing is recorded."""
    with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
        tenant = session.get(Tenant, tenant_id)
        assert tenant is not None
        name, current = tenant.name, tenant.status
        # persist=False: opening a page must not write a readiness_check row.
        report = evaluate_readiness(session, tenant_id, persist=False)

    rank: dict[str, int] = {CheckStatus.FAIL: 0, CheckStatus.WARN: 1, CheckStatus.PASS: 2}
    ordered = sorted(report.outcomes, key=lambda o: (rank.get(o.status, 3), o.check_id))
    rows = [
        f"""<tr>
          <td><strong class="mono">{_esc(o.check_id)}</strong>{_detail(o.detail)}</td>
          <td>{status_badge(o.status)}</td>
          <td class="num">{_number(o.measured_value)}</td>
          <td class="num">{_number(o.threshold)}</td>
        </tr>"""
        # Blocking first, then warnings, the same flagged-first order the review queue uses.
        for o in ordered
    ]
    passed = sum(1 for o in report.outcomes if o.status == CheckStatus.PASS)

    if report.passed:
        verdict = banner(f"<strong>Ready for pilot.</strong> No fail-severity check is outstanding{f'; {len(report.warnings)} warning(s) recorded' if report.warnings else ''}.", tone="ok")
    else:
        verdict = banner(f"<strong>{len(report.failures)} check(s) blocking.</strong> {_esc(', '.join(o.check_id for o in report.failures))}", tone="alert")

    summary = f"""<div class="grid cols-3">
      {stat("Passing", f"{passed}/{len(report.outcomes)}", hint=progress(passed, len(report.outcomes)), tone="ok" if report.passed else "", icon_name="check")}
      {stat("Blocking", str(len(report.failures)), hint="fail-severity checks", tone="danger" if report.failures else "ok", icon_name="alert")}
      {stat("Warnings", str(len(report.warnings)), hint="recorded, not blocking", tone="warn" if report.warnings else "", icon_name="flag")}
    </div>"""
    checks = card(table(("Check", "Status", "Measured", "Threshold"), rows, numeric=(2, 3)), title="Every check", subtitle="Evaluated now and not recorded. A <code>warn</code> does not block the pilot transition; a <code>fail</code> does, and <code>onboarding &rarr; pilot</code> re-runs these checks rather than trusting this page.", icon_name="readiness", cls="flush")
    return _page(f"Readiness — {name}", f"""{verdict}{summary}<div class="section">{checks}</div>""", admin=admin, active="labs", eyebrow=f"Lab · {current}", crumbs=_lab_crumbs(tenant_id, name, "Readiness"), actions=f'<a class="btn ghost" href="/admin/labs/{tenant_id}">{icon("back")}Back to {_esc(name)}</a>')


@router.post("/labs/{tenant_id}/assign")
def assign_step(tenant_id: uuid.UUID, request: Request, admin: CurrentAdmin, task_key: Annotated[str, Form()], model_definition_id: Annotated[uuid.UUID, Form()]) -> Response:
    try:
        with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
            propose_assignment(session, tenant_id=tenant_id, task_key=task_key, model_definition_id=model_definition_id, actor_id=admin.platform_user_id)
    except ConfigRefused as exc:
        return _redirect(f"/admin/labs/{tenant_id}", error=exc.reason)
    return _redirect(f"/admin/labs/{tenant_id}", notice=f"Proposed a model for {task_key}")


@router.post("/labs/{tenant_id}/assignments/{assignment_id}/activate")
def activate_step(tenant_id: uuid.UUID, assignment_id: uuid.UUID, request: Request, admin: CurrentAdmin) -> Response:
    try:
        with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
            activate_assignment(session, assignment_id=assignment_id, tenant_id=tenant_id, actor_id=admin.platform_user_id)
    except (UngatedActivation, ModelResolutionError) as exc:
        return _redirect(f"/admin/labs/{tenant_id}", error=str(exc))
    return _redirect(f"/admin/labs/{tenant_id}", notice="Assignment activated")


# =============================================================== providers ===
@router.get("/providers", response_class=HTMLResponse)
def providers_page(admin: CurrentAdmin, error: str | None = None, notice: str | None = None) -> HTMLResponse:
    with read_session() as session:
        providers = session.execute(sa.select(ModelProvider).order_by(ModelProvider.name)).scalars().all()
        definitions = session.execute(sa.select(ModelDefinition, ModelProvider).join(ModelProvider, ModelProvider.id == ModelDefinition.provider_id).order_by(ModelProvider.name, ModelDefinition.display_name)).all()

        provider_rows = [
            f"""<tr><td><strong>{_esc(p.name)}</strong></td><td>{badge("cloud API", "info") if p.kind == ProviderKind.CLOUD_API else badge("locally hosted", "brand")}</td>
                <td><code>{_esc(p.api_key_env_var or "—")}</code></td>
                <td class="mono">{_esc(p.default_endpoint or "—")}</td>
                <td>{badge("global", "muted", dot=False) if p.tenant_id is None else badge("one lab", "warn", dot=False)}</td></tr>"""
            for p in providers
        ]
        definition_rows = [
            f"""<tr><td><strong>{_esc(d.display_name)}</strong></td><td><code>{_esc(d.model_identifier)}</code></td>
                <td>{_esc(p.name)}</td>
                <td class="mono">{_esc(d.endpoint_override or "—")}</td>
                <td>{badge("global", "muted", dot=False) if d.tenant_id is None else badge("one lab", "warn", dot=False)}</td></tr>"""
            for d, p in definitions
        ]
        options = "".join(f'<option value="{p.id}">{_esc(p.name)}</option>' for p in providers)
        provider_count, model_count = len(providers), len(definitions)
        local_count = sum(1 for p in providers if p.kind == ProviderKind.LOCAL_OPENAI_COMPATIBLE)

    add_provider_form = (
        card(
            f"""<form method="post" action="/admin/providers">
 <div class="form-grid">
 <div><label for="pname">Name</label><input id="pname" name="name" required></div>
 <div><label for="kind">Kind</label>
 <select id="kind" name="kind">
 <option value="cloud_api">Cloud API</option>
 <option value="local_openai_compatible">Locally hosted (OpenAI-compatible)</option>
 </select></div>
 <div><label for="env">API key environment variable</label><input id="env" name="api_key_env_var" placeholder="ANTHROPIC_API_KEY"><div class="hint">Cloud only. The name is stored; the key never reaches the database.</div></div>
 <div><label for="endpoint">Endpoint</label><input id="endpoint" name="default_endpoint" placeholder="http://10.0.0.5:8000/v1"><div class="hint">Locally hosted only.</div></div>
 </div>
 <div class="actions"><button class="primary" type="submit">{icon("plus")}Add provider</button></div>
 </form>""",
            title="Add a provider",
            icon_name="plus",
        )
        if _can(admin, "POST", "/admin/providers")
        else ""
    )
    add_model_form = (
        card(
            f"""<form method="post" action="/admin/models">
 <div class="form-grid">
 <div><label for="provider_id">Provider</label><select id="provider_id" name="provider_id" required>{options}</select></div>
 <div><label for="ident">Model identifier</label><input id="ident" name="model_identifier" placeholder="claude-sonnet-5" required></div>
 <div><label for="dname">Display name</label><input id="dname" name="display_name" required></div>
 <div><label for="override">Endpoint override</label><input id="override" name="endpoint_override"><div class="hint">One lab's local box.</div></div>
 </div>
 <p class="hint">Identifiers carry no date suffix — <code>claude-sonnet-5</code>, not <code>claude-sonnet-5-20260415</code>; the API rejects the latter.</p>
 <div class="actions"><button class="primary" type="submit">{icon("plus")}Add model</button></div>
 </form>""",
            title="Add a model",
            icon_name="plus",
        )
        if _can(admin, "POST", "/admin/models")
        else ""
    )
    kpis = f"""<div class="grid cols-3">
      {stat("Providers", str(provider_count), hint=f"{local_count} locally hosted", icon_name="providers")}
      {stat("Models", str(model_count), hint="in the catalog", icon_name="spark")}
      {stat("Keys stored", "0", hint="keys stay in the server environment", tone="ok", icon_name="key")}
    </div>"""
    body = f"""{_messages(error, notice)}{kpis}
 <div class="section">{card(table(("Provider", "Kind", "Key from env", "Endpoint", "Scope"), provider_rows, empty_text="No providers yet.", empty_icon="providers"), title="Providers", icon_name="providers", cls="flush")}</div>
 <div class="section">{card(table(("Name", "Identifier", "Provider", "Endpoint override", "Scope"), definition_rows, empty_text="No models yet.", empty_icon="spark"), title="Models", icon_name="spark", cls="flush")}</div>
 <div class="section grid cols-2">{add_provider_form}{add_model_form}</div>"""
    return _page("Models & providers", body, admin=admin, active="providers", eyebrow="Workspace", subtitle="Which vendors and locally hosted boxes the pipeline may call, and the models each one serves.")


@router.post("/providers")
def add_provider(admin: CurrentAdmin, name: Annotated[str, Form()], kind: Annotated[str, Form()], api_key_env_var: Annotated[str | None, Form()] = None, default_endpoint: Annotated[str | None, Form()] = None) -> Response:
    with system_session() as session:
        try:
            create_provider(session, name=name, kind=kind, api_key_env_var=api_key_env_var or None, default_endpoint=default_endpoint or None, actor_id=admin.platform_user_id)
        except ConfigRefused as exc:
            return _redirect("/admin/providers", error=exc.reason)
    return _redirect("/admin/providers", notice=f"Added provider {name}")


@router.post("/models")
def add_model(admin: CurrentAdmin, provider_id: Annotated[uuid.UUID, Form()], model_identifier: Annotated[str, Form()], display_name: Annotated[str, Form()], endpoint_override: Annotated[str | None, Form()] = None) -> Response:
    with system_session() as session:
        try:
            create_definition(session, provider_id=provider_id, model_identifier=model_identifier, display_name=display_name, endpoint_override=endpoint_override or None, actor_id=admin.platform_user_id)
        except ConfigRefused as exc:
            return _redirect("/admin/providers", error=exc.reason)
    return _redirect("/admin/providers", notice=f"Added model {display_name}")


# ============================================================== onboarding ===
#: The extra inputs a step's button carries; every other step runs with no options.
_STEP_OPTIONS = {"lexicon-mine": ' <input name="min_frequency" type="number" min="1" max="9999" placeholder="min. frequency" style="width:9em" aria-label="Minimum frequency">', "boilerplate-mine": ' <label class="check" style="margin:0"><input type="checkbox" name="verified_only" value="1"> verified mappings only</label>'}


@router.get("/labs/{tenant_id}/onboarding", response_class=HTMLResponse)
def onboarding_page(tenant_id: uuid.UUID, request: Request, admin: CurrentAdmin, error: str | None = None, notice: str | None = None) -> HTMLResponse:
    """Where this lab stands in onboarding, and the steps a product admin runs for it."""
    base = f"/admin/labs/{tenant_id}/onboarding"
    can_upload = _can(admin, "POST", f"{base}/roster")
    can_run = _can(admin, "POST", f"{base}/steps/derive-map")
    with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
        tenant = session.get(Tenant, tenant_id)
        assert tenant is not None
        name = tenant.name
        overview = onboarding_steps.onboarding_overview(session, tenant_id)

    batch_rows = [
        f"""<tr><td><code>{_esc(b["id"][:8])}</code></td><td>{_esc(b["batch_type"])}</td><td>{badge(b["stage"], "brand", dot=False)}</td>
            <td>{status_badge(b["status"])}</td><td class="num">{_esc(b["accepted"])}</td><td class="num">{badge(str(b["blocking_issue_count"]), "danger") if b["blocking_issue_count"] else "0"}</td>
            <td>{f'<form class="inline" method="post" action="{base}/batches/{_esc(b["id"])}/merge-proposals"><button type="submit" class="linkish">propose merges</button></form>' if can_run and b["batch_type"] == ImportBatchType.TEMPLATE else ""}</td></tr>"""
        for b in overview["recent_batches"]
    ]
    readiness = overview["readiness"]
    verification = overview["corpus_verification"]
    gold_items = overview["gold_progress"].items()
    gold_done = sum(v["annotated"] for _, v in gold_items)
    gold_target = sum(v["target"] for _, v in gold_items)
    gold_detail = ", ".join(f"{k}: {v['annotated']}/{v['target']}" for k, v in gold_items) or "none yet"

    kpis = f"""<div class="grid cols-4">
      {stat("Readiness", "Ready" if readiness["passed"] else f"{len(readiness['failures'])} blocking", hint=_esc("ready for pilot" if readiness["passed"] else ", ".join(readiness["failures"])[:90]), tone="ok" if readiness["passed"] else "danger", icon_name="readiness")}
      {stat("Corpus verified", f"{verification['verified']}/{verification['target']}", hint=progress(verification["verified"], verification["target"]), icon_name="doc")}
      {stat("Gold transcripts", f"{gold_done}/{gold_target}" if gold_target else "0", hint=_esc(gold_detail[:90]), icon_name="mic")}
      {stat("Critical rules", str(overview["active_critical_rules"]), hint="active and approved", icon_name="alert")}
    </div>"""

    uploads = (
        card(
            f"""<div class="grid cols-2">
 <form method="post" action="{base}/roster" enctype="multipart/form-data">
 <h3>{icon("users")} Roster</h3><p class="meta">The HR CSV export of every person at the lab.</p>
 <label for="roster" class="sr-only">Roster (HR CSV export)</label>
 <input id="roster" name="file" type="file" accept=".csv,text/csv" required>
 <div class="actions"><button type="submit" class="sm">{icon("upload")}Import roster</button></div>
 </form>
 <form method="post" action="{base}/templates" enctype="multipart/form-data">
 <h3>{icon("doc")} Report templates</h3><p class="meta">Word or text documents, one per template.</p>
 <label for="templates" class="sr-only">Report templates (documents)</label>
 <input id="templates" name="files" type="file" multiple required>
 <div class="actions"><button type="submit" class="sm">{icon("upload")}Submit templates</button></div>
 </form>
 <form method="post" action="{base}/shorthand" enctype="multipart/form-data">
 <h3>{icon("lexicon")} Shorthand reference</h3><p class="meta">Optional. The sheet transcriptionists use: <code>LLL = left lower lobe</code>.</p>
 <label for="shorthand" class="sr-only">Shorthand reference (PDF, Word or text)</label>
 <input id="shorthand" name="files" type="file" accept=".pdf,.docx,.txt,.md,.csv" multiple required>
 <div class="actions"><button type="submit" class="sm">{icon("upload")}Add shorthand</button></div>
 </form>
 <form method="post" action="{base}/corpus" enctype="multipart/form-data">
 <h3>{icon("database")} Signed reports</h3><p class="meta">CSV with a report_text column, or a JSON array.</p>
 <label for="corpus" class="sr-only">Historical signed reports</label>
 <input id="corpus" name="file" type="file" accept=".csv,.json,text/csv,application/json" required>
 <div class="actions"><button type="submit" class="sm">{icon("upload")}Load reports</button></div>
 </form>
 </div>
 <p class="hint">Mark rows <code>is_deidentified</code> only if they truly are: it decides whether a report may ever reach an external model.</p>""",
            title="Upload data",
            subtitle="Each upload opens an import batch; nothing goes live until the lab's radiologists approve it.",
            icon_name="upload",
        )
        if can_upload
        else ""
    )
    step_forms = "".join(f'<form class="inline step-form" method="post" action="{base}/steps/{key}"><button type="submit" class="sm">{icon("play")}{_esc(label)}</button>{_STEP_OPTIONS.get(key, "")}</form>' for key, (label, _fn) in onboarding_steps.STEPS.items())
    run = card(f'<div class="row" style="gap:10px">{step_forms}</div><p class="hint" style="margin-top:14px">Clinical approvals (template candidates, merges, collision findings, critical rules) are made by the lab\'s radiologists on the lab side, not here.</p>', title="Run a step", subtitle="Mining and seeding steps, in the order onboarding usually runs them.", icon_name="play") if can_run else ""
    batches = card(table(("Batch", "Type", "Stage", "Status", "Accepted", "Blocking", ""), batch_rows, empty_text="No batches yet. Upload a roster to start.", empty_icon="jobs", numeric=(4, 5)), title="Recent batches", icon_name="jobs", cls="flush")
    body = f"""{_messages(error, notice)}{kpis}<div class="section">{uploads}</div><div class="section">{run}</div><div class="section">{batches}</div>"""
    return _page(f"Onboarding — {name}", body, admin=admin, active="labs", eyebrow="Lab onboarding", crumbs=_lab_crumbs(tenant_id, name, "Onboarding"), actions=f'<a class="btn" href="/admin/labs/{tenant_id}/readiness">{icon("readiness")}Readiness</a>')


def _summarise(result: dict) -> str:
    """A one-line notice from a step's counts."""
    parts = [f"{k.replace('_', ' ')}: {v}" for k, v in result.items() if isinstance(v, int | float | str) and k != "batch_id"]
    return "; ".join(parts) or "done"


@router.post("/labs/{tenant_id}/onboarding/roster")
async def upload_roster(tenant_id: uuid.UUID, request: Request, admin: CurrentAdmin, file: Annotated[UploadFile, File()]) -> Response:
    data = await file.read()
    try:
        with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
            result = onboarding_steps.import_roster_file(session, tenant_id, data)
    except StepRefused as exc:
        return _redirect(f"/admin/labs/{tenant_id}/onboarding", error=exc.reason)
    return _redirect(f"/admin/labs/{tenant_id}/onboarding", notice=f"Roster imported — {_summarise(result)}")


@router.post("/labs/{tenant_id}/onboarding/templates")
async def upload_templates(tenant_id: uuid.UUID, request: Request, admin: CurrentAdmin, files: Annotated[list[UploadFile], File()]) -> Response:
    uploads = [ArtifactUpload(filename=f.filename or "unnamed", data=await f.read(), mime_type=f.content_type) for f in files]
    try:
        with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
            result = onboarding_steps.submit_template_files(session, tenant_id, uploads)
    except StepRefused as exc:
        return _redirect(f"/admin/labs/{tenant_id}/onboarding", error=exc.reason)
    return _redirect(f"/admin/labs/{tenant_id}/onboarding", notice=f"Templates submitted — {_summarise(result)}")


@router.post("/labs/{tenant_id}/onboarding/shorthand")
async def upload_shorthand(tenant_id: uuid.UUID, request: Request, admin: CurrentAdmin, files: Annotated[list[UploadFile], File()]) -> Response:
    uploads = [ArtifactUpload(filename=f.filename or "unnamed", data=await f.read(), mime_type=f.content_type) for f in files]
    try:
        with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
            result = onboarding_steps.submit_shorthand_files(session, tenant_id, uploads)
    except StepRefused as exc:
        return _redirect(f"/admin/labs/{tenant_id}/onboarding", error=exc.reason)
    conflicts = f"; {len(result['conflicts'])} conflict(s) for the lab's radiologists to settle" if result["conflicts"] else ""
    return _redirect(f"/admin/labs/{tenant_id}/onboarding", notice=f"Shorthand added — {result['mappings']} pair(s), {result['terms_created']} new term(s), {result['terms_updated']} updated{conflicts}")


@router.post("/labs/{tenant_id}/onboarding/corpus")
async def upload_corpus(tenant_id: uuid.UUID, request: Request, admin: CurrentAdmin, file: Annotated[UploadFile, File()]) -> Response:
    data = await file.read()
    try:
        with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
            result = onboarding_steps.load_corpus_file(session, tenant_id, data, file.filename or "corpus.csv")
    except StepRefused as exc:
        return _redirect(f"/admin/labs/{tenant_id}/onboarding", error=exc.reason)
    skipped = f"; {len(result['problems'])} row(s) skipped" if result["problems"] else ""
    return _redirect(f"/admin/labs/{tenant_id}/onboarding", notice=f"Reports loaded — {_summarise(result)}{skipped}")


@router.post("/labs/{tenant_id}/onboarding/batches/{batch_id}/merge-proposals")
def merge_proposals(tenant_id: uuid.UUID, batch_id: uuid.UUID, request: Request, admin: CurrentAdmin) -> Response:
    try:
        with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
            result = onboarding_steps.propose_template_merges(session, tenant_id, batch_id)
    except StepRefused as exc:
        return _redirect(f"/admin/labs/{tenant_id}/onboarding", error=exc.reason)
    return _redirect(f"/admin/labs/{tenant_id}/onboarding", notice=f"Merge proposals — {_summarise(result)}")


@router.post("/labs/{tenant_id}/onboarding/steps/{step}")
def run_onboarding_step(tenant_id: uuid.UUID, step: str, request: Request, admin: CurrentAdmin, min_frequency: Annotated[int | None, Form()] = None, verified_only: Annotated[str | None, Form()] = None) -> Response:
    """Run one step from its button; the two steps with options read them from the form."""
    options: dict[str, object] = {}
    if min_frequency is not None:
        options["min_frequency"] = min_frequency
    if verified_only:
        options["verified_only"] = True
    try:
        with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
            result = onboarding_steps.run_step(session, tenant_id, step, options)
    except StepRefused as exc:
        return _redirect(f"/admin/labs/{tenant_id}/onboarding", error=exc.reason)
    label = onboarding_steps.STEPS[step][0]
    return _redirect(f"/admin/labs/{tenant_id}/onboarding", notice=f"{label} — {_summarise(result)}")


# ================================================================== users ===
@router.get("/users", response_class=HTMLResponse)
def users_page(admin: CurrentAdmin, error: str | None = None, notice: str | None = None, page: int | None = None) -> HTMLResponse:
    """Who can sign in to this panel, and in which role."""
    can_create = _can(admin, "POST", "/admin/users")
    sample = uuid.UUID(int=0)
    can_toggle = _can(admin, "POST", f"/admin/users/{sample}/deactivate")
    can_reset = _can(admin, "POST", f"/admin/users/{sample}/password")
    with read_session() as session:
        role_counts = dict(session.execute(sa.select(PlatformUser.role, sa.func.count()).where(PlatformUser.is_active.is_(True)).group_by(PlatformUser.role)).all())
        all_count = session.execute(sa.select(sa.func.count()).select_from(PlatformUser)).scalar_one()
        paged = paginate(session, users.platform_users_query(), Page.of(page, 25))
        accounts = paged.rows
        rows = []
        for u in accounts:
            actions = []
            if can_toggle and u.id != admin.platform_user_id:
                verb = "deactivate" if u.is_active else "reactivate"
                actions.append(f'<form class="inline" method="post" action="/admin/users/{u.id}/{verb}"><button type="submit" class="sm{" danger" if verb == "deactivate" else ""}">{verb.title()}</button></form>')
            if can_reset:
                actions.append(f'<form class="inline" method="post" action="/admin/users/{u.id}/password"><input name="password" type="password" minlength="{auth.MIN_PASSWORD_LENGTH}" placeholder="new password" required autocomplete="new-password" aria-label="New password for {_esc(u.display_name)}"><button type="submit" class="sm">Set password</button></form>')
            initials = "".join(w[0] for w in u.display_name.split()[:2]).upper() or "?"
            rows.append(
                f"""<tr><td><div class="row" style="gap:10px;flex-wrap:nowrap"><span class="avatar">{_esc(initials)}</span><div><strong>{_esc(u.display_name)}</strong>{" " + badge("you", "brand", dot=False) if u.id == admin.platform_user_id else ""}<div class="cell-sub">{_esc(u.email)}</div></div></div></td>
                <td>{badge(u.role.replace("_", " "), "info" if u.role == PlatformRole.PRODUCT_ADMIN else "muted", dot=False)}</td><td>{badge("active", "ok") if u.is_active else badge("inactive", "warn")}</td>
                <td class="nowrap">{_esc(u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "never")}</td>
                <td><div class="row">{"".join(actions)}</div></td></tr>"""
            )
        active_count = sum(role_counts.values())
        admins = role_counts.get(PlatformRole.PRODUCT_ADMIN, 0)

    create_form = (
        card(
            f"""<form method="post" action="/admin/users">
 <div class="form-grid">
 <div><label for="u_name">Display name</label><input id="u_name" name="display_name" required></div>
 <div><label for="u_email">Email</label><input id="u_email" name="email" type="email" required></div>
 <div><label for="u_role">Role</label>
 <select id="u_role" name="role">
 <option value="{PlatformRole.SUPPORT}">support (read-only)</option>
 <option value="{PlatformRole.PRODUCT_ADMIN}">product admin</option>
 </select></div>
 <div><label for="u_password">Initial password</label>
 <input id="u_password" name="password" type="password" minlength="{auth.MIN_PASSWORD_LENGTH}" required autocomplete="new-password"><div class="hint">At least {auth.MIN_PASSWORD_LENGTH} characters.</div></div>
 </div>
 <div class="actions"><button class="primary" type="submit">{icon("plus")}Add account</button></div>
 </form>""",
            title="Add an account",
            icon_name="plus",
        )
        if can_create
        else ""
    )
    kpis = f"""<div class="grid cols-3">
      {stat("Accounts", str(all_count), hint=f"{active_count} active", icon_name="users")}
      {stat("Product admins", str(admins), hint="active, can change configuration", icon_name="shield")}
      {stat("Support", str(role_counts.get(PlatformRole.SUPPORT, 0)), hint="active, read-only access", icon_name="account")}
    </div>"""
    accounts_card = card(table(("Name", "Role", "Status", "Last sign-in", ""), rows, empty_icon="users") + pager(page=paged.page.number, pages=paged.pages, total=paged.total, path="/admin/users"), title="Accounts", subtitle="Deactivating an account or setting its password signs it out everywhere. Which role may do what is set in <code>radreport/api/access_policy.xml</code>.", icon_name="users", cls="flush")
    return _page("Platform users", f"""{_messages(error, notice)}{kpis}<div class="section">{accounts_card}</div><div class="section">{create_form}</div>""", admin=admin, active="users", eyebrow="People", subtitle="Who can sign in to this panel, and in which role.")


@router.post("/users")
def create_user(admin: CurrentAdmin, display_name: Annotated[str, Form()], email: Annotated[str, Form()], role: Annotated[str, Form()], password: Annotated[str, Form()]) -> Response:
    with system_session() as session:
        try:
            user = users.create_platform_user(session, email=email, display_name=display_name, role=role, password=password, actor_id=admin.platform_user_id)
        except users.UserChangeRefused as exc:
            return _redirect("/admin/users", error=exc.reason)
        added = f"Added {user.role} {user.email}"
    return _redirect("/admin/users", notice=added)


def _set_active(user_id: uuid.UUID, active: bool, admin: AuthenticatedAdmin) -> Response:
    with system_session() as session:
        try:
            user = users.set_active(session, user_id=user_id, active=active, actor_id=admin.platform_user_id)
        except users.UserChangeRefused as exc:
            return _redirect("/admin/users", error=exc.reason)
        done = f"{user.email} {'reactivated' if active else 'deactivated'}"
    return _redirect("/admin/users", notice=done)


@router.post("/users/{user_id}/deactivate")
def deactivate_user(user_id: uuid.UUID, admin: CurrentAdmin) -> Response:
    return _set_active(user_id, False, admin)


@router.post("/users/{user_id}/reactivate")
def reactivate_user(user_id: uuid.UUID, admin: CurrentAdmin) -> Response:
    return _set_active(user_id, True, admin)


@router.post("/users/{user_id}/password")
def reset_password(user_id: uuid.UUID, admin: CurrentAdmin, password: Annotated[str, Form()]) -> Response:
    with system_session() as session:
        try:
            user = users.reset_password(session, user_id=user_id, password=password, actor_id=admin.platform_user_id)
        except users.UserChangeRefused as exc:
            return _redirect("/admin/users", error=exc.reason)
        done = f"Password set for {user.email}; their sessions were ended"
    # Setting your own password ends your own session too, so sign in again.
    if user_id == admin.platform_user_id:
        return _redirect("/admin/login", error="Your password changed; sign in again")
    return _redirect("/admin/users", notice=done)


# ================================================================ account ===
@router.get("/account", response_class=HTMLResponse)
def account_page(admin: CurrentAdmin, error: str | None = None) -> HTMLResponse:
    """Your own account: change your password."""
    who = card(facts((("Name", _esc(admin.display_name)), ("Role", badge(admin.role.replace("_", " "), "info", dot=False)), ("Access", "change configuration" if admin.role == PlatformRole.PRODUCT_ADMIN else "read-only"))), title="Profile", icon_name="account")
    change = card(
        f"""<form class="stack" method="post" action="/admin/account/password">
 <label for="current_password">Current password</label>
 <input id="current_password" name="current_password" type="password" required autocomplete="current-password">
 <label for="new_password">New password</label>
 <input id="new_password" name="new_password" type="password" minlength="{auth.MIN_PASSWORD_LENGTH}" required autocomplete="new-password">
 <div class="hint">At least {auth.MIN_PASSWORD_LENGTH} characters. Changing it signs you out everywhere, this browser included.</div>
 <div class="actions"><button class="primary" type="submit">{icon("key")}Change password</button></div>
 </form>""",
        title="Change your password",
        icon_name="key",
    )
    return _page("Your account", f"""{_messages(error, None)}<div class="grid cols-2">{who}{change}</div>""", admin=admin, active="account", eyebrow="People")


@router.post("/account/password")
def change_own_password(admin: CurrentAdmin, current_password: Annotated[str, Form()], new_password: Annotated[str, Form()]) -> Response:
    with system_session() as session:
        try:
            users.change_own_password(session, user_id=admin.platform_user_id, current=current_password, new=new_password)
        except users.UserChangeRefused as exc:
            return _redirect("/admin/account", error=exc.reason)
    response = _redirect("/admin/login", error="Password changed; sign in with the new one")
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response
