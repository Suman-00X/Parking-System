"""The admin panel's operations pages: settings ops may change without a release.

Order: the settings page, platform-wide or for one lab (config_page) -> change or clear one
value (set_config_value, reset_config_value).
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from radreport.api.deps import CurrentAdmin
from radreport.api.routes.admin_panel import _can, _messages, _page, _redirect
from radreport.api.routes.ops import config_session
from radreport.api.ui import badge, card, esc, icon, table
from radreport.core import system_config
from radreport.core.types import TenantStatus
from radreport.db.models.tenancy import Tenant
from radreport.db.session import system_session

router = APIRouter(prefix="/admin", tags=["admin-ops-panel"])

_SOURCE_TONES = {"lab": "brand", "platform": "info", "environment": "warn", "default": "muted"}
_SOURCE_LABELS = {"lab": "this lab", "platform": "platform-wide", "environment": "environment", "default": "code default"}


def _fmt(value: float | int | None, kind: type) -> str:
    if value is None:
        return "&mdash;"
    if kind is int:
        return str(int(value))
    return f"{float(value):g}"


def _back(lab: uuid.UUID | None) -> str:
    return f"/admin/config?lab={lab}" if lab else "/admin/config"


@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request, admin: CurrentAdmin, lab: uuid.UUID | None = None, error: str | None = None, notice: str | None = None) -> HTMLResponse:
    """Every operational setting, its effective value and where it comes from."""
    with system_session() as session:
        labs = [(t.id, t.name) for t in session.execute(sa.select(Tenant).where(Tenant.status != TenantStatus.OFFBOARDED).order_by(Tenant.name)).scalars().all()]
    lab_name = next((n for i, n in labs if i == lab), None)
    if lab is not None and lab_name is None:
        return _redirect("/admin/config", error="no such lab")
    with config_session(admin, lab, request) as session:
        settings = system_config.resolve_all(session, tenant_id=lab)

    can_write = _can(admin, "POST", "/admin/config/adapter.global_hours")
    scope_field = f'<input type="hidden" name="lab" value="{lab}">' if lab else ""
    rows = []
    for r in settings:
        spec = system_config.SETTINGS[r.key]
        step = "1" if spec.kind is int else "any"
        controls = ""
        if can_write:
            stored = r.lab_value if lab else r.platform_value
            reset = f'<form class="inline" method="post" action="/admin/config/{esc(r.key)}/reset">{scope_field}<button type="submit" class="sm ghost">Reset</button></form>' if stored is not None else ""
            controls = f'<div class="row"><form class="inline" method="post" action="/admin/config/{esc(r.key)}">{scope_field}<input name="value" type="number" step="{step}" min="{spec.minimum:g}" max="{spec.maximum:g}" value="{_fmt(r.value, spec.kind)}" required aria-label="{esc(spec.label)}" style="width:7.5em"><button type="submit" class="sm">Save</button></form>{reset}</div>'
        rows.append(
            f"""<tr><td><strong>{esc(spec.label)}</strong><div class="cell-sub">{esc(spec.description)}</div><div class="cell-sub mono">{esc(r.key)} · env <code>{esc(spec.env_var)}</code></div></td>
            <td class="num"><strong style="font-size:16px">{_fmt(r.value, spec.kind)}</strong></td>
            <td>{badge(_SOURCE_LABELS[r.source], _SOURCE_TONES[r.source])}</td>
            <td class="num">{_fmt(spec.kind(spec.default), spec.kind)}</td>
            <td>{controls}</td></tr>"""
        )

    options = "".join(f'<option value="{i}"{" selected" if i == lab else ""}>{esc(n)}</option>' for i, n in labs)
    scope = f"""<form method="get" action="/admin/config" class="row" style="gap:10px">
      <label for="lab" style="margin:0">Showing</label>
      <select id="lab" name="lab" onchange="this.form.submit()" style="width:auto;min-width:220px"><option value="">Platform-wide values</option>{options}</select>
      <noscript><button type="submit" class="sm">Show</button></noscript>
    </form>"""
    explain = card(
        """<div class="grid cols-4">
        <div><span class="badge brand">this lab</span><p class="meta" style="margin-top:8px">A value stored for one lab. Wins over everything.</p></div>
        <div><span class="badge info">platform-wide</span><p class="meta" style="margin-top:8px">Stored here for every lab without its own value.</p></div>
        <div><span class="badge warn">environment</span><p class="meta" style="margin-top:8px">Read from the server's environment variable when nothing is stored.</p></div>
        <div><span class="badge muted">code default</span><p class="meta" style="margin-top:8px">What ships in the code, when nothing else is set.</p></div>
      </div>""",
        title="Where a value comes from",
        subtitle="Most specific first. Every change is written to the audit log.",
        icon_name="info",
    )
    body = f"""{_messages(error, notice)}{explain}<div class="section">{card(table(("Setting", "Effective", "Source", "Default", "Change"), rows, numeric=(1, 3)), title="Speech adaptation gates" + (f" — {lab_name}" if lab_name else ""), subtitle="The thresholds the adapter gates compare a lab's verbatim corpus against before any training run.", icon_name="config", actions=scope, cls="flush")}</div>"""
    return _page("System settings", body, admin=admin, active="config", eyebrow="Operations", subtitle="Thresholds ops can tune for a pilot or a lab without an engineering change.", actions=f'<a class="btn ghost" href="/admin/api/ops/config{f"?tenant_id={lab}" if lab else ""}">{icon("doc")}JSON</a>')


@router.post("/config/{key}")
def set_config_value(key: str, request: Request, admin: CurrentAdmin, value: Annotated[float, Form()], lab: Annotated[uuid.UUID | None, Form()] = None) -> Response:
    try:
        with config_session(admin, lab, request) as session:
            resolved = system_config.set_value(session, key, value, actor_id=admin.platform_user_id, tenant_id=lab)
    except system_config.SettingRefused as exc:
        return _redirect(_back(lab), error=str(exc))
    return _redirect(_back(lab), notice=f"{system_config.SETTINGS[key].label} set to {resolved.value:g}")


@router.post("/config/{key}/reset")
def reset_config_value(key: str, request: Request, admin: CurrentAdmin, lab: Annotated[uuid.UUID | None, Form()] = None) -> Response:
    try:
        with config_session(admin, lab, request) as session:
            resolved = system_config.reset_value(session, key, actor_id=admin.platform_user_id, tenant_id=lab)
    except system_config.SettingRefused as exc:
        return _redirect(_back(lab), error=str(exc))
    return _redirect(_back(lab), notice=f"{system_config.SETTINGS[key].label} reset; now {resolved.value:g} from {_SOURCE_LABELS[resolved.source]}")
