"""The admin panel's operations pages: what the pipeline costs, and settings ops may change without a release.

Order: spend across labs, or one lab's by stage, with the spikes marked (costs_page) -> PgBouncer's
connection pools, with waiting clients and busy pools flagged (pools_page) -> the settings page, platform-wide or for one lab (config_page) -> change or clear one value
(set_config_value, reset_config_value).
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
from radreport.api.routing import BridgedRoute
from radreport.api.ui import badge, bar_list, card, empty, esc, icon, line_chart, stat, table
from radreport.core import system_config
from radreport.core.types import TenantStatus
from radreport.db.models.tenancy import Tenant
from radreport.db.session import read_session, system_session
from radreport.monitoring import costs

router = APIRouter(prefix="/admin", tags=["admin-ops-panel"], route_class=BridgedRoute)

_SOURCE_TONES = {"lab": "brand", "platform": "info", "environment": "warn", "default": "muted"}
_SOURCE_LABELS = {"lab": "this lab", "platform": "platform-wide", "environment": "environment", "default": "code default"}


def _fmt(value: float | int | None, kind: type) -> str:
    if value is None:
        return "&mdash;"
    if kind is int:
        return str(int(value))
    return f"{float(value):g}"


_PERIODS = (7, 30, 90)


def _money(value: float) -> str:
    return f"${value:,.4f}" if 0 < value < 1 else f"${value:,.2f}"


def _change(change: float | None) -> str:
    if change is None:
        return '<span class="muted">no earlier spend</span>'
    direction = "up" if change > 0 else "down"
    return f'<span class="delta {direction}">{"▲" if change > 0 else "▼"} {abs(change):.0%}</span> <span class="muted">vs previous period</span>'


def _spike_rows(spikes: list[costs.Spike], *, lab: str | None = None) -> list[str]:
    return [f"<tr>{f'<td><strong>{esc(lab)}</strong></td>' if lab is not None else ''}<td class='nowrap'>{esc(s.day.isoformat())}</td><td class='num'><strong>{_money(s.cost_usd)}</strong></td><td class='num'>{_money(s.baseline_usd)}</td><td class='num'>{badge(f'{s.ratio:g}x', 'danger')}</td></tr>" for s in spikes]


def _stage_table(stages: list[dict]) -> str:
    rows = [
        f"""<tr><td><strong class="mono">{esc(st["stage_name"])}</strong><div class="cell-sub">{esc(st["task_key"] or "deterministic")}</div></td>
        <td class="num">{st["executions"]:,}</td><td class="num"><strong>{_money(st["cost_usd"])}</strong></td>
        <td class="num">{st["tokens_in"]:,} / {st["tokens_out"]:,}</td>
        <td class="num">{badge(f"{st['cache_hit_ratio']:.0%}", "ok" if st["cache_hit_ratio"] >= 0.5 else "warn" if st["tokens_in"] else "muted", dot=False)}</td>
        <td class="num">{st["avg_duration_ms"]:,.0f} ms</td><td class="num">{badge(str(st["failures"]), "danger") if st["failures"] else "0"}</td></tr>"""
        for st in stages
    ]
    return table(("Stage", "Runs", "Spend", "Tokens in / out", "Cache hits", "Avg time", "Failures"), rows, empty_text="No stage has run in this period.", empty_icon="ops", numeric=(1, 2, 3, 4, 5, 6))


@router.get("/costs", response_class=HTMLResponse)
def costs_page(admin: CurrentAdmin, lab: uuid.UUID | None = None, days: int | None = None) -> HTMLResponse:
    """Spend across every lab, or one lab's by stage, with the days that cost far more than usual."""
    period = days if days in _PERIODS else 30
    with read_session() as session:
        names = {t.id: t.name for t in session.execute(sa.select(Tenant).order_by(Tenant.name)).scalars().all()}
        if lab is not None and lab not in names:
            return _redirect("/admin/costs")
        summary = costs.lab_summary(session, lab, days=period) if lab else costs.platform_summary(session, days=period)

    tabs = "".join(f'<a href="/admin/costs?{f"lab={lab}&" if lab else ""}days={d}" class="{"active" if d == period else ""}">{d} days</a>' for d in _PERIODS)
    options = "".join(f'<option value="{i}"{" selected" if i == lab else ""}>{esc(n)}</option>' for i, n in names.items())
    controls = f"""<div class="spread"><div class="tabs">{tabs}</div>
      <form method="get" action="/admin/costs" class="row" style="gap:8px"><input type="hidden" name="days" value="{period}">
      <select name="lab" onchange="this.form.submit()" style="width:auto;min-width:220px" aria-label="Lab"><option value="">All labs</option>{options}</select><noscript><button class="sm">Show</button></noscript></form></div>"""
    chart_points = [(d.strftime("%d %b"), v) for d, v in summary["series"]]
    spike_days = {s.day for s in summary["spikes"]} | {s.day for entry in summary.get("labs", []) for s in entry.spikes}
    anomaly_index = [i for i, (d, _v) in enumerate(summary["series"]) if d in spike_days]
    trend = card(f'{line_chart(chart_points, anomalies=anomaly_index)}<div class="legend" style="margin-top:10px"><span><i></i>daily spend</span><span><i class="anomaly"></i>spike: over {costs.SPIKE_RATIO:g}x the trailing {costs.SPIKE_WINDOW_DAYS}-day median and {costs.SPIKE_SIGMAS:g}σ above its mean</span></div>', title="Daily spend", subtitle=f"{esc(summary['start'].isoformat())} to {esc(summary['end'].isoformat())}, shadow runs excluded", icon_name="cost")
    stages = card(_stage_table(summary["stages"]), title="Where the money goes", subtitle="Per pipeline stage. Cache hits are prompt-cache reads as a share of input tokens; a low share on a model stage is worth a look.", icon_name="ops", cls="flush")
    by_stage_bars = card(bar_list([(st["stage_name"], st["cost_usd"]) for st in summary["stages"] if st["cost_usd"]][:10]), title="Spend by stage", icon_name="spark")

    if lab:
        kpis = f"""<div class="grid cols-4">
          {stat("Spend", _money(summary["total_cost_usd"]), hint=_change(summary["change"]), icon_name="cost")}
          {stat("Reports", f"{summary['runs']:,}", hint=f"{summary['failed_runs']} failed runs", icon_name="doc")}
          {stat("Per report", _money(summary["avg_cost_per_report_usd"]), hint=f"most expensive run {_money(summary['max_run_cost_usd'])}", icon_name="spark")}
          {stat("Spikes", str(len(summary["spikes"])), hint=f"{summary['budget_hits']} run(s) hit the budget cap", tone="danger" if summary["spikes"] or summary["budget_hits"] else "ok", icon_name="alert")}
        </div>"""
        alerts = card(table(("Day", "Spend", "Usual", "Ratio"), _spike_rows(summary["spikes"]), numeric=(1, 2, 3), empty_text="No unusual days in this period.", empty_icon="check"), title="Spikes", icon_name="alert", cls="flush")
        body = f"""{controls}<div class="section">{kpis}</div><div class="section grid cols-2">{trend}{by_stage_bars}</div><div class="section">{stages}</div><div class="section">{alerts}</div>"""
        return _page(f"Cost — {names[lab]}", body, admin=admin, active="costs", eyebrow="Operations", crumbs=(("Cost & usage", "/admin/costs"), (names[lab], None)), actions=f'<a class="btn ghost" href="/admin/api/costs?tenant_id={lab}&days={period}">{icon("doc")}JSON</a>')

    labs = summary["labs"]
    lab_rows = [
        f"""<tr><td><strong><a href="/admin/costs?lab={entry.tenant_id}&days={period}">{esc(entry.name)}</a></strong></td>
        <td class="num"><strong>{_money(entry.cost_usd)}</strong></td><td class="num">{entry.runs:,}</td><td class="num">{_money(entry.avg_cost_usd)}</td>
        <td>{_change(entry.change)}</td><td class="num">{badge(str(len(entry.spikes)), "danger") if entry.spikes else "0"}</td></tr>"""
        for entry in labs
    ]
    all_spikes = [row for entry in labs for row in _spike_rows(entry.spikes, lab=entry.name)]
    kpis = f"""<div class="grid cols-4">
      {stat("Platform spend", _money(summary["total_cost_usd"]), hint=_change(summary["change"]), icon_name="cost")}
      {stat("Reports", f"{summary['runs']:,}", hint=f"across {len(labs)} lab(s)", icon_name="doc")}
      {stat("Per report", _money(summary["avg_cost_per_report_usd"]), hint="the unit economics that matter", icon_name="spark")}
      {stat("Spikes", str(len(all_spikes)), hint="labs with an unusual day", tone="danger" if all_spikes else "ok", icon_name="alert")}
    </div>"""
    spike_card = card(table(("Lab", "Day", "Spend", "Usual", "Ratio"), all_spikes, numeric=(2, 3, 4), empty_text="No lab had an unusual day in this period.", empty_icon="check"), title="Spikes", subtitle="Also sent as cost.anomaly events every six hours.", icon_name="alert", cls="flush")
    top = card(bar_list([(entry.name, entry.cost_usd) for entry in labs[:10]]), title="Top labs by spend", icon_name="labs")
    body = (
        f"""{controls}<div class="section">{kpis}</div>
      <div class="section grid cols-2">{trend}{top}</div>
      <div class="section">{card(table(("Lab", "Spend", "Reports", "Per report", "Trend", "Spikes"), lab_rows, numeric=(1, 2, 3, 5), empty_text="No pipeline runs in this period.", empty_icon="cost"), title="By lab", icon_name="labs", cls="flush")}</div>
      <div class="section">{spike_card}</div><div class="section">{stages}</div>"""
        if summary["runs"] or labs
        else f"{controls}<div class='section'>{kpis}</div><div class='section'>{card(empty('Pipeline runs appear here as soon as labs start dictating.', title='No spend yet', icon_name='cost'))}</div>"
    )
    return _page("Cost & usage", body, admin=admin, active="costs", eyebrow="Operations", subtitle="What every lab's reports cost to produce, where in the pipeline the money goes, and the days that cost far more than usual.", actions=f'<a class="btn ghost" href="/admin/api/costs?days={period}">{icon("doc")}JSON</a>')


@router.get("/pools", response_class=HTMLResponse)
def pools_page(admin: CurrentAdmin) -> HTMLResponse:
    """PgBouncer's pools as it sees them now: clients active and waiting, server connections in use."""
    from radreport.db.pgbouncer_stats import pool_report

    report = pool_report()
    actions = f'<a class="btn ghost" href="/admin/api/ops/pgbouncer">{icon("doc")}JSON</a>'
    subtitle = "Read from PgBouncer's admin console on every load. Clients waiting means every server connection in the pool is busy."
    if not report["enabled"] or not report.get("reachable"):
        body = card(empty(report["reason"], title="No pool data", icon_name="database"))
        return _page("Connection pools", body, admin=admin, active="pools", eyebrow="Operations", subtitle=subtitle, actions=actions)

    limits, pools = report["limits"], report["pools"]
    size = limits["default_pool_size"]
    rows = [
        f"""<tr><td><strong class="mono">{esc(p["database"])}</strong><div class="meta">{esc(p["user"])} · {esc(p["pool_mode"])}</div></td>
        <td class="num">{p["clients_active"]}</td><td class="num">{badge(str(p["clients_waiting"]), "danger") if p["clients_waiting"] else "0"}</td>
        <td class="num">{badge(f"{p['servers_active']}/{size}", "warn") if p["busy"] else f"{p['servers_active']}/{size}"}</td><td class="num">{p["servers_idle"]}</td><td class="num">{p["servers_used"]}</td>
        <td class="num">{_fmt(p["max_wait_ms"], float)}</td></tr>"""
        for p in pools
    ]
    waiting = sum(p["clients_waiting"] for p in pools)
    kpis = f"""<div class="grid cols-4">
      {stat("Clients", f"{sum(p['clients_active'] for p in pools):,}", hint=f"up to {limits['max_client_conn']:,} allowed", icon_name="users")}
      {stat("Waiting", str(waiting), hint=", ".join(esc(d) for d in report["waiting"]) or "no client is queued", tone="danger" if waiting else "ok", icon_name="alert")}
      {stat("Server connections", str(sum(p["servers_active"] + p["servers_idle"] + p["servers_used"] for p in pools)), hint=f"{size} per pool, {limits['reserve_pool_size']} in reserve", icon_name="database")}
      {stat("Busy pools", str(len(report["busy"])), hint=f"at or above {report['busy_share']:.0%} of the pool in use", tone="warn" if report["busy"] else "ok", icon_name="flag")}
    </div>"""
    body = f"""{kpis}<div class="section">{card(table(("Pool", "Clients active", "Waiting", "Servers active", "Idle", "Used", "Longest wait (ms)"), rows, numeric=(1, 2, 3, 4, 5, 6), empty_text="PgBouncer has no pools for the app yet.", empty_icon="database"), title="Pools", icon_name="database", cls="flush")}</div>"""
    return _page("Connection pools", body, admin=admin, active="pools", eyebrow="Operations", subtitle=subtitle, actions=actions)


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
