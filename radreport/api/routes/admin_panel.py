"""The admin panel's pages, rendered on the server.

Order: sign in (login_page, login_submit, logout_submit) -> manage labs (home, labs_page,
create_lab, lab_page, set_lab_user_password, change_status, lab_readiness_page) -> configure models per step
(assign_step, activate_step, providers_page, add_provider, add_model) -> onboard a lab
(onboarding_page, upload_roster, upload_templates, upload_corpus, merge_proposals, run_onboarding_step) ->
manage platform users (users_page, create_user, deactivate_user, reactivate_user, reset_password).
"""

from __future__ import annotations

import html
import re
import uuid
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
from radreport.api.routes.admin_api import SLUG_PATTERN, lab_user_out
from radreport.auth import lab as lab_auth
from radreport.core.config import get_settings
from radreport.core.errors import ModelResolutionError, UngatedActivation
from radreport.core.tenancy import TenantTransitionError, allowed_transitions
from radreport.core.types import CheckStatus, ImportBatchType, PlatformRole, ProviderKind, TenantStatus
from radreport.db.models.identity import AppUser
from radreport.db.models.modelconfig import ModelDefinition, ModelProvider
from radreport.db.models.tenancy import Tenant
from radreport.db.session import system_session
from radreport.onboarding.batches import ArtifactUpload
from radreport.onboarding.readiness import evaluate_readiness
from radreport.onboarding.registration import LabRegistration, register_lab, transition_status

router = APIRouter(prefix="/admin", tags=["admin-panel"])


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


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
    return (f'<div class="err">{_esc(error)}</div>' if error else "") + (f'<div class="banner ok">{_esc(notice)}</div>' if notice else "")


def _page(title: str, body: str, *, admin: AuthenticatedAdmin | None = None) -> HTMLResponse:
    nav = f'<div class="meta">{_esc(admin.display_name)} <span class="tag ungrounded">{_esc(admin.role)}</span> · <a href="/admin/labs">Labs</a> · <a href="/admin/providers">Providers</a> · <a href="/admin/users">Users</a> · <form method="post" action="/admin/logout" style="display:inline"><button type="submit" class="linkish">Sign out</button></form></div>' if admin else ""
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · radreport admin</title>
<link rel="stylesheet" href="/ui/static/review.css">
<style>
  .linkish {{ background:none; border:none; color:inherit; text-decoration:underline;
              cursor:pointer; font:inherit; padding:0; }}
  table {{ border-collapse:collapse; width:100%; margin:12px 0; }}
  th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line);
           font-size:14px; vertical-align:top; }}
  th {{ color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase;
        letter-spacing:.04em; }}
  input,select {{ font:inherit; padding:6px 8px; border-radius:4px;
                  border:1px solid var(--line); background:var(--bg); color:var(--fg); }}
  label {{ display:block; margin:10px 0 4px; font-size:13px; color:var(--muted); }}
  form.stack {{ max-width:460px; }}
  form.inline {{ display:inline; }}
  h2 {{ font-size:16px; margin-top:28px; }}
  .err {{ color:var(--critical); background:var(--critical-bg); padding:10px 12px;
          border-radius:6px; margin:12px 0; }}
  .tag.ok {{ color:var(--ok); }}
  .banner.ok {{ background:var(--bg); color:var(--ok); border:1px solid var(--ok); }}
</style>
</head><body><main>
<h1>{_esc(title)}</h1>
{nav}
{body}
</main></body></html>""")


# =================================================================== login ===
_LOGIN_TABS_STYLE = """<style>
  .tabs { display:flex; gap:4px; border-bottom:1px solid var(--line); margin:16px 0; max-width:460px; }
  .tabs button { background:none; border:none; border-bottom:2px solid transparent; color:var(--muted);
                 font:inherit; padding:8px 12px; cursor:pointer; margin-bottom:-1px; }
  .tabs button[aria-selected="true"] { color:var(--fg); border-bottom-color:var(--fg); font-weight:600; }
  .cred { border:1px solid var(--line); border-radius:6px; padding:10px 12px; margin-bottom:8px; max-width:460px; }
  .cred code { font-size:13px; }
</style>"""

_LOGIN_TABS_SCRIPT = """<script>
  const tabs = document.querySelectorAll('.tabs button');
  tabs.forEach(tab => tab.addEventListener('click', () => {
    tabs.forEach(t => { t.setAttribute('aria-selected', t === tab); document.getElementById(t.dataset.panel).hidden = t !== tab; });
  }));
  document.querySelectorAll('[data-email]').forEach(button => button.addEventListener('click', () => {
    document.getElementById('email').value = button.dataset.email;
    document.getElementById('password').value = button.dataset.password;
    tabs[0].click();
  }));
</script>"""


def _demo_credentials_panel() -> str:
    """The test-credentials tab body, one card per configured demo account."""
    cards = "".join(
        f"""<div class="cred"><div class="label">{_esc(a.label)} <span class="tag ungrounded">{_esc(a.role)}</span></div>
      <div>Email: <code>{_esc(a.email)}</code></div><div>Password: <code>{_esc(a.password)}</code></div>
      <div class="actions"><button type="button" data-email="{_esc(a.email)}" data-password="{_esc(a.password)}">Use these</button></div></div>"""
        for a in get_settings().demo_accounts
    )
    return f'<p class="meta">Demo accounts for trying the product. They hold synthetic data only.</p>{cards}'


@router.get("/login", response_class=HTMLResponse)
def login_page(error: str | None = None) -> HTMLResponse:
    form = """<form class="stack" method="post" action="/admin/login">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" required autocomplete="username">
      <label for="password">Password</label>
      <input id="password" name="password" type="password" required
             autocomplete="current-password">
      <div class="actions"><button class="primary" type="submit">Sign in</button></div>
    </form>"""
    if not get_settings().demo_accounts:
        return _page("Sign in", f"{_messages(error, None)}\n    {form}")
    return _page(
        "Sign in",
        f"""{_LOGIN_TABS_STYLE}{_messages(error, None)}
    <div class="tabs" role="tablist">
      <button type="button" role="tab" aria-selected="true" data-panel="panel-signin">Sign in</button>
      <button type="button" role="tab" aria-selected="false" data-panel="panel-demo">Test credentials</button>
    </div>
    <div id="panel-signin" role="tabpanel">{form}</div>
    <div id="panel-demo" role="tabpanel" hidden>{_demo_credentials_panel()}</div>
    {_LOGIN_TABS_SCRIPT}""",
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
def labs_page(admin: CurrentAdmin, error: str | None = None, notice: str | None = None, show: str | None = None) -> HTMLResponse:
    """The lab list; offboarded labs are hidden unless asked for."""
    show_all = show == "all"
    with system_session() as session:
        query = sa.select(Tenant).order_by(Tenant.name)
        if not show_all:
            query = query.where(Tenant.status != TenantStatus.OFFBOARDED)
        tenants = list(session.execute(query).scalars().all())
        rows = "".join(
            f"""<tr>
              <td><a href="/admin/labs/{t.id}">{_esc(t.name)}</a></td>
              <td>{_esc(t.slug)}</td>
              <td>{_esc(t.status)}</td>
              <td>{"yes" if t.training_pooling_consent else "no"}</td>
            </tr>"""
            for t in tenants
        )

    toggle = '<a href="/admin/labs">hide offboarded</a>' if show_all else '<a href="/admin/labs?show=all">show offboarded</a>'
    empty = "<p class='meta'>No labs yet.</p>" if not tenants else ""
    register = (
        f"""<h2>Onboard a lab</h2>
 <form class="stack" method="post" action="/admin/labs">
 <label for="name">Lab name</label>
 <input id="name" name="name" required>
 <label for="slug">Slug</label>
 <input id="slug" name="slug" pattern="{_esc(SLUG_PATTERN.strip("^$"))}" required>
 <label for="admin_display_name">Lab administrator</label>
 <input id="admin_display_name" name="admin_display_name" required>
 <label for="admin_email">Their email</label>
 <input id="admin_email" name="admin_email" type="email" required>
 <label for="admin_employee_code">Their employee code</label>
 <input id="admin_employee_code" name="admin_employee_code" required>
 <label>
 <input type="checkbox" name="training_pooling_consent" value="1">
 The signed contract includes the cross-tenant training pooling clause
 </label>
 <label for="training_consent_ref">Contract reference for that clause</label>
 <input id="training_consent_ref" name="training_consent_ref" placeholder="required if the box is ticked">
 <label for="patient_notice_version">Patient-notice wording version</label>
 <input id="patient_notice_version" name="patient_notice_version">
 <p class="meta">Pooling consent is captured here because registration is the one
 moment the answer is actually known; it is near-impossible to retrofit later.</p>
 <div class="actions"><button class="primary" type="submit">Register</button></div>
 </form>"""
        if _can(admin, "POST", "/admin/labs")
        else ""
    )
    return _page(
        "Labs",
        f"""{_messages(error, notice)}
 <div class="meta">{toggle}</div>
 <table>
 <tr><th>Name</th><th>Slug</th><th>Status</th><th>Pooling consent</th></tr>
 {rows}
 </table>{empty}
 {register}""",
        admin=admin,
    )


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


@router.get("/labs/{tenant_id}", response_class=HTMLResponse)
def lab_page(tenant_id: uuid.UUID, request: Request, admin: CurrentAdmin, error: str | None = None, notice: str | None = None) -> HTMLResponse:
    """One lab: its status, its pipeline steps, and what serves each of them."""
    can_assign = _can(admin, "POST", f"/admin/labs/{tenant_id}/assign")
    can_activate = _can(admin, "POST", f"/admin/labs/{tenant_id}/assignments/{uuid.UUID(int=0)}/activate")
    can_change_status = _can(admin, "POST", f"/admin/labs/{tenant_id}/status")
    with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
        tenant = session.get(Tenant, tenant_id)
        assert tenant is not None
        steps = step_configuration(session, tenant_id=tenant_id)
        models = available_models(session, tenant_id=tenant_id)
        options = "".join(f'<option value="{d.id}">{_esc(d.display_name)} — {_esc(p.name)}{" (local)" if p.kind == ProviderKind.LOCAL_OPENAI_COMPATIBLE else ""}</option>' for d, p in models)

        rows = []
        for step in steps:
            active = f"{_esc(step.active_model)} <span class='tag ungrounded'>{_esc(step.active_provider)}</span>" if step.is_configured else "<span class='tag flag'>not configured</span>"
            pending = "".join(f"<div class='meta'>proposed: {_esc(label)}" + (f" <form class='inline' method='post' action='/admin/labs/{tenant_id}/assignments/{aid}/activate'><button type='submit' class='linkish'>activate</button></form>" if can_activate else "") + "</div>" for aid, label in step.proposed)
            bucket = "<span class='tag critical'>consequential</span>" if step.task_bucket == "consequential" else "<span class='tag ungrounded'>bounded</span>"
            kind = "ASR engine" if step.is_asr else "LLM"
            change = (
                f"""<form method="post" action="/admin/labs/{tenant_id}/assign">
                      <input type="hidden" name="task_key" value="{_esc(step.task_key)}">
                      <select name="model_definition_id" required>
                        <option value="">choose a model…</option>{options}
                      </select>
                      <button type="submit">Propose</button>
                    </form>"""
                if can_assign
                else ""
            )
            rows.append(f"<tr><td><strong>{_esc(step.task_key)}</strong><div class='meta'>{kind}</div></td><td>{bucket}</td><td>{active}{pending}</td><td>{change}</td></tr>")

        targets = sorted(allowed_transitions(tenant.status))
        name, slug, current, consent = tenant.name, tenant.slug, tenant.status, tenant.training_pooling_consent
        staff = [lab_user_out(u) for u in session.execute(sa.select(AppUser).where(AppUser.tenant_id == tenant_id).order_by(AppUser.display_name)).scalars().all()]

    can_set_password = _can(admin, "POST", f"/admin/labs/{tenant_id}/users/{uuid.UUID(int=0)}/password")
    staff_rows = "".join(
        f"""<tr><td>{_esc(u.display_name)}<div class="meta">{_esc(u.email or "no email")} · {_esc(u.employee_code)}</div></td>
            <td>{_esc(", ".join(u.roles))}</td>
            <td>{"can sign in" if u.can_sign_in else "<span class='tag flag'>cannot sign in</span>"}</td>
            <td>{_esc(u.last_login_at[:16].replace("T", " ") if u.last_login_at else "never")}</td>
            <td>{f'<form class="inline" method="post" action="/admin/labs/{tenant_id}/users/{u.id}/password"><input name="password" type="password" minlength="{auth.MIN_PASSWORD_LENGTH}" placeholder="new password" required autocomplete="new-password"> <button type="submit" class="linkish">set password</button></form>' if can_set_password and u.email else ""}</td></tr>"""
        for u in staff
    )

    unconfigured = sum(1 for s in steps if not s.is_configured)
    warning = f"<div class='banner blocked'>{unconfigured} step(s) have no active model. A pipeline run will fail at the first of them.</div>" if unconfigured else ""
    status_form = (
        f"""<form class="inline" method="post" action="/admin/labs/{tenant_id}/status">
 <select name="status" required>{"".join(f'<option value="{_esc(t)}">{_esc(t)}</option>' for t in targets)}</select>
 <button type="submit">Move</button></form>"""
        if can_change_status and targets
        else ""
    )
    return _page(
        name,
        f"""{_messages(error, notice)}{warning}
 <div class="meta">{_esc(slug)} · status: <strong>{_esc(current)}</strong> {status_form} ·
 pooling consent: {"yes" if consent else "no"}</div>
 <p class="meta"><a href="/admin/labs/{tenant_id}/onboarding">Onboarding</a> ·
 <a href="/admin/labs/{tenant_id}/readiness">Readiness</a> (gates the move from onboarding to pilot)</p>
 <p class="meta">Cloud API keys are read from the server environment and are
 never entered or stored here. A locally hosted model is configured by its
 address on the <a href="/admin/providers">providers</a> page.</p>
 <table>
 <tr><th>Pipeline step</th><th>Bucket</th><th>Serving</th><th>Change</th></tr>
 {"".join(rows)}
 </table>
 <p class="meta">A proposal does not go live. Activation requires a gold-set
 <code>eval_run</code> for that model; without one it is refused.</p>
 <h2>Lab staff</h2>
 <table>
 <tr><th>Name</th><th>Roles</th><th>Sign-in</th><th>Last sign-in</th><th></th></tr>
 {staff_rows or '<tr><td colspan="5" class="meta">No staff yet; import a roster from the onboarding page.</td></tr>'}
 </table>
 <p class="meta">Lab staff sign in at <code>POST /auth/login</code> with this lab's slug
 (<code>{_esc(slug)}</code>), their email and password. Setting a password signs that person out everywhere.</p>""",
        admin=admin,
    )


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
        name, slug, current = tenant.name, tenant.slug, tenant.status
        # persist=False: opening a page must not write a readiness_check row.
        report = evaluate_readiness(session, tenant_id, persist=False)

    tag: dict[str, str] = {CheckStatus.FAIL: "critical", CheckStatus.WARN: "flag", CheckStatus.PASS: "ok"}
    rank: dict[str, int] = {CheckStatus.FAIL: 0, CheckStatus.WARN: 1, CheckStatus.PASS: 2}
    rows = "".join(
        f"""<tr>
          <td><strong>{_esc(o.check_id)}</strong>{_detail(o.detail)}</td>
          <td><span class="tag {tag.get(o.status, "ungrounded")}">{_esc(o.status)}</span></td>
          <td>{_number(o.measured_value)}</td>
          <td>{_number(o.threshold)}</td>
        </tr>"""
        # Blocking first, then warnings, the same flagged-first order the review queue uses.
        for o in sorted(report.outcomes, key=lambda o: (rank.get(o.status, 3), o.check_id))
    )

    if report.passed:
        banner = f"<div class='banner ok'><strong>Ready for pilot.</strong> No fail-severity check is outstanding{f'; {len(report.warnings)} warning(s) recorded' if report.warnings else ''}.</div>"
    else:
        banner = f"<div class='banner alert'><strong>{len(report.failures)} check(s) blocking.</strong> {_esc(', '.join(o.check_id for o in report.failures))}</div>"

    return _page(
        f"Readiness — {name}",
        f"""{banner}
 <div class="meta"><a href="/admin/labs/{tenant_id}">&larr; {_esc(name)}</a> ·
 {_esc(slug)} · {_esc(current)}</div>
 <table>
 <tr><th>Check</th><th>Status</th><th>Measured</th><th>Threshold</th></tr>
 {rows}
 </table>
 <p class="meta">Evaluated now and not recorded. A <code>warn</code> does not
 block the pilot transition; a <code>fail</code> does, and
 <code>onboarding &rarr; pilot</code> re-runs these checks rather than trusting
 this page.</p>""",
        admin=admin,
    )


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
    with system_session() as session:
        providers = session.execute(sa.select(ModelProvider).order_by(ModelProvider.name)).scalars().all()
        definitions = session.execute(sa.select(ModelDefinition, ModelProvider).join(ModelProvider, ModelProvider.id == ModelDefinition.provider_id).order_by(ModelProvider.name, ModelDefinition.display_name)).all()

        provider_rows = "".join(
            f"""<tr><td>{_esc(p.name)}</td><td>{_esc(p.kind)}</td>
                <td>{_esc(p.api_key_env_var or "—")}</td>
                <td>{_esc(p.default_endpoint or "—")}</td>
                <td>{"global" if p.tenant_id is None else "one lab"}</td></tr>"""
            for p in providers
        )
        definition_rows = "".join(
            f"""<tr><td>{_esc(d.display_name)}</td><td><code>{_esc(d.model_identifier)}</code></td>
                <td>{_esc(p.name)}</td>
                <td>{_esc(d.endpoint_override or "—")}</td>
                <td>{"global" if d.tenant_id is None else "one lab"}</td></tr>"""
            for d, p in definitions
        )
        options = "".join(f'<option value="{p.id}">{_esc(p.name)}</option>' for p in providers)

    add_provider_form = (
        """<h2>Add a provider</h2>
 <form class="stack" method="post" action="/admin/providers">
 <label for="pname">Name</label><input id="pname" name="name" required>
 <label for="kind">Kind</label>
 <select id="kind" name="kind">
 <option value="cloud_api">Cloud API</option>
 <option value="local_openai_compatible">Locally hosted (OpenAI-compatible)</option>
 </select>
 <label for="env">Environment variable holding the API key (cloud only)</label>
 <input id="env" name="api_key_env_var" placeholder="ANTHROPIC_API_KEY">
 <label for="endpoint">Endpoint (locally hosted only)</label>
 <input id="endpoint" name="default_endpoint" placeholder="http://10.0.0.5:8000/v1">
 <div class="actions"><button class="primary" type="submit">Add provider</button></div>
 </form>"""
        if _can(admin, "POST", "/admin/providers")
        else ""
    )
    add_model_form = (
        f"""<form class="stack" method="post" action="/admin/models">
 <label for="provider_id">Provider</label>
 <select id="provider_id" name="provider_id" required>{options}</select>
 <label for="ident">Model identifier</label>
 <input id="ident" name="model_identifier" placeholder="claude-sonnet-5" required>
 <label for="dname">Display name</label><input id="dname" name="display_name" required>
 <label for="override">Endpoint override (one lab's local box)</label>
 <input id="override" name="endpoint_override">
 <div class="actions"><button class="primary" type="submit">Add model</button></div>
 </form>
 <p class="meta">Identifiers carry no date suffix — <code>claude-sonnet-5</code>,
 not <code>claude-sonnet-5-20260415</code>; the API rejects the latter.</p>"""
        if _can(admin, "POST", "/admin/models")
        else ""
    )
    return _page(
        "Providers and models",
        f"""{_messages(error, notice)}
 <table>
 <tr><th>Provider</th><th>Kind</th><th>Key from env</th><th>Endpoint</th><th>Scope</th></tr>
 {provider_rows}
 </table>
 <p class="meta">The <em>name</em> of the environment variable is stored; the
 key itself never reaches the database.</p>
 {add_provider_form}
 <h2>Models</h2>
 <table>
 <tr><th>Name</th><th>Identifier</th><th>Provider</th><th>Endpoint override</th>
 <th>Scope</th></tr>
 {definition_rows}
 </table>
 {add_model_form}""",
        admin=admin,
    )


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

    batch_rows = "".join(
        f"""<tr><td><code>{_esc(b["id"][:8])}</code></td><td>{_esc(b["batch_type"])}</td><td>{_esc(b["stage"])}</td>
            <td>{_esc(b["status"])}</td><td>{_esc(b["accepted"])}</td><td>{_esc(b["blocking_issue_count"])}</td>
            <td>{f'<form class="inline" method="post" action="{base}/batches/{_esc(b["id"])}/merge-proposals"><button type="submit" class="linkish">propose merges</button></form>' if can_run and b["batch_type"] == ImportBatchType.TEMPLATE else ""}</td></tr>"""
        for b in overview["recent_batches"]
    )
    readiness = overview["readiness"]
    readiness_line = "ready for pilot" if readiness["passed"] else f"blocking: {', '.join(readiness['failures'])}"
    gold = ", ".join(f"{k}: {v['annotated']}/{v['target']}" for k, v in overview["gold_progress"].items()) or "none yet"

    uploads = (
        f"""<h2>Upload data</h2>
 <form class="stack" method="post" action="{base}/roster" enctype="multipart/form-data">
 <label for="roster">Roster (HR CSV export)</label>
 <input id="roster" name="file" type="file" accept=".csv,text/csv" required>
 <div class="actions"><button type="submit">Import roster</button></div>
 </form>
 <form class="stack" method="post" action="{base}/templates" enctype="multipart/form-data">
 <label for="templates">Report templates (documents)</label>
 <input id="templates" name="files" type="file" multiple required>
 <div class="actions"><button type="submit">Submit templates</button></div>
 </form>
 <form class="stack" method="post" action="{base}/corpus" enctype="multipart/form-data">
 <label for="corpus">Historical signed reports (CSV with a report_text column, or a JSON array)</label>
 <input id="corpus" name="file" type="file" accept=".csv,.json,text/csv,application/json" required>
 <div class="actions"><button type="submit">Load reports</button></div>
 </form>
 <p class="meta">Mark rows <code>is_deidentified</code> only if they truly are: it decides whether
 a report may ever reach an external model.</p>"""
        if can_upload
        else ""
    )
    steps = "<h2>Run a step</h2>" + "".join(f'<form class="inline" method="post" action="{base}/steps/{key}"><button type="submit">{_esc(label)}</button></form> ' for key, (label, _fn) in onboarding_steps.STEPS.items()) + "<p class='meta'>Clinical approvals (template candidates, merges, collision findings, critical rules) are made by the lab's radiologists on the lab side, not here.</p>" if can_run else ""
    return _page(
        f"Onboarding — {name}",
        f"""{_messages(error, notice)}
 <div class="meta"><a href="/admin/labs/{tenant_id}">&larr; {_esc(name)}</a> ·
 <a href="/admin/labs/{tenant_id}/readiness">readiness</a>: {_esc(readiness_line)}</div>
 <table>
 <tr><th>Corpus mappings verified</th><td>{overview["corpus_verification"]["verified"]} / {overview["corpus_verification"]["target"]}</td></tr>
 <tr><th>Gold transcripts</th><td>{_esc(gold)}</td></tr>
 <tr><th>Active critical rules</th><td>{overview["active_critical_rules"]}</td></tr>
 </table>
 <h2>Recent batches</h2>
 <table>
 <tr><th>Batch</th><th>Type</th><th>Stage</th><th>Status</th><th>Accepted</th><th>Blocking</th><th></th></tr>
 {batch_rows or '<tr><td colspan="7" class="meta">No batches yet.</td></tr>'}
 </table>
 {uploads}
 {steps}""",
        admin=admin,
    )


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
def run_onboarding_step(tenant_id: uuid.UUID, step: str, request: Request, admin: CurrentAdmin) -> Response:
    try:
        with admin_lab_session(admin, tenant_id, ip_address=client_ip(request)) as session:
            result = onboarding_steps.run_step(session, tenant_id, step)
    except StepRefused as exc:
        return _redirect(f"/admin/labs/{tenant_id}/onboarding", error=exc.reason)
    label = onboarding_steps.STEPS[step][0]
    return _redirect(f"/admin/labs/{tenant_id}/onboarding", notice=f"{label} — {_summarise(result)}")


# ================================================================== users ===
@router.get("/users", response_class=HTMLResponse)
def users_page(admin: CurrentAdmin, error: str | None = None, notice: str | None = None) -> HTMLResponse:
    """Who can sign in to this panel, and in which role."""
    can_create = _can(admin, "POST", "/admin/users")
    sample = uuid.UUID(int=0)
    can_toggle = _can(admin, "POST", f"/admin/users/{sample}/deactivate")
    can_reset = _can(admin, "POST", f"/admin/users/{sample}/password")
    with system_session() as session:
        accounts = users.list_platform_users(session)
        rows = []
        for u in accounts:
            actions = []
            if can_toggle and u.id != admin.platform_user_id:
                verb = "deactivate" if u.is_active else "reactivate"
                actions.append(f'<form class="inline" method="post" action="/admin/users/{u.id}/{verb}"><button type="submit" class="linkish">{verb}</button></form>')
            if can_reset:
                actions.append(f'<form class="inline" method="post" action="/admin/users/{u.id}/password"><input name="password" type="password" minlength="{auth.MIN_PASSWORD_LENGTH}" placeholder="new password" required autocomplete="new-password"> <button type="submit" class="linkish">set password</button></form>')
            rows.append(
                f"""<tr><td>{_esc(u.display_name)}{" (you)" if u.id == admin.platform_user_id else ""}<div class="meta">{_esc(u.email)}</div></td>
                <td>{_esc(u.role)}</td><td>{"active" if u.is_active else "<span class='tag flag'>inactive</span>"}</td>
                <td>{_esc(u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "never")}</td>
                <td>{" · ".join(actions)}</td></tr>"""
            )

    create_form = (
        f"""<h2>Add an account</h2>
 <form class="stack" method="post" action="/admin/users">
 <label for="u_name">Display name</label><input id="u_name" name="display_name" required>
 <label for="u_email">Email</label><input id="u_email" name="email" type="email" required>
 <label for="u_role">Role</label>
 <select id="u_role" name="role">
 <option value="{PlatformRole.SUPPORT}">support (read-only)</option>
 <option value="{PlatformRole.PRODUCT_ADMIN}">product admin</option>
 </select>
 <label for="u_password">Initial password (at least {auth.MIN_PASSWORD_LENGTH} characters)</label>
 <input id="u_password" name="password" type="password" minlength="{auth.MIN_PASSWORD_LENGTH}" required autocomplete="new-password">
 <div class="actions"><button class="primary" type="submit">Add account</button></div>
 </form>"""
        if can_create
        else ""
    )
    return _page(
        "Platform users",
        f"""{_messages(error, notice)}
 <table>
 <tr><th>Name</th><th>Role</th><th>Status</th><th>Last sign-in</th><th></th></tr>
 {"".join(rows)}
 </table>
 <p class="meta">Deactivating an account or setting its password signs it out everywhere.
 Which role may do what is set in <code>radreport/api/access_policy.xml</code>.</p>
 {create_form}""",
        admin=admin,
    )


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
