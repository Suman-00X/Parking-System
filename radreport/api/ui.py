"""The building blocks every server-rendered page is made from, so the admin panel and the lab screens look like one product.

Defines: escaping (esc), the icon set (icon), the page frames (admin_page, lab_page, auth_page),
and the components pages compose (flash, card, stat, badge, table, empty, progress, steps,
facts, line_chart, bar_list).
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from fastapi.responses import HTMLResponse


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


# ------------------------------------------------------------------ icons ---
#: 24x24 stroke icons, drawn with currentColor so they follow the theme.
_ICONS: dict[str, str] = {
    "logo": '<path d="M3 12h2l2-6 3 12 3-9 2 6 2-3h4"/>',
    "labs": '<path d="M9 3h6"/><path d="M10 3v6L4.5 18.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3"/><path d="M7.5 15h9"/>',
    "providers": '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7"/><path d="M18 14a6 6 0 0 1 3.5 6"/>',
    "account": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "cost": '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 6-7"/><path d="M20 7v4h-4" transform="translate(0 0)"/>',
    "ops": '<path d="M22 12h-4l-3 8L9 4l-3 8H2"/>',
    "config": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3"/><path d="M1 14h6M9 8h6M17 16h6"/>',
    "jobs": '<path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>',
    "queue": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.7 4H7.3a2 2 0 0 0-1.8 1.1Z"/>',
    "lexicon": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5v14Z"/><path d="M20 17v4H6.5A2.5 2.5 0 0 1 4 18.5"/><path d="M9 7h7M9 11h5"/>',
    "readiness": '<path d="M9 12l2 2 4-4"/><path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3Z"/>',
    "onboarding": '<path d="M12 3v12"/><path d="m7 8 5-5 5 5"/><path d="M5 21h14"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "moon": '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "close": '<path d="M18 6 6 18M6 6l12 12"/>',
    "alert": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
    "play": '<circle cx="12" cy="12" r="9"/><path d="m10 8 6 4-6 4V8Z"/>',
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "back": '<path d="M19 12H5M11 18l-6-6 6-6"/>',
    "shield": '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3Z"/>',
    "mic": '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v5M8 22h8"/>',
    "spark": '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "database": '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    "doc": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "key": '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 9.3-9.3M16 7l3 3M14 9l2 2"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    "flag": '<path d="M4 22V4M4 4h12l-2 4 2 4H4"/>',
}


def icon(name: str, *, label: str | None = None) -> str:
    """An inline SVG icon; decorative unless given a label."""
    a11y = f'role="img" aria-label="{esc(label)}"' if label else 'aria-hidden="true"'
    return f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" {a11y}>{_ICONS.get(name, _ICONS["info"])}</svg>'


def _initials(name: str) -> str:
    parts = [p for p in name.replace("@", " ").split() if p[:1].isalnum()]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


# ----------------------------------------------------------------- frames ---
@dataclass(frozen=True, slots=True)
class NavItem:
    key: str
    label: str
    href: str
    icon: str
    count: int | None = None


ADMIN_NAV: tuple[tuple[str, tuple[NavItem, ...]], ...] = (("Workspace", (NavItem("labs", "Labs", "/admin/labs", "labs"), NavItem("providers", "Models & providers", "/admin/providers", "providers"))), ("Operations", (NavItem("costs", "Cost & usage", "/admin/costs", "cost"), NavItem("config", "System settings", "/admin/config", "config"))), ("People", (NavItem("users", "Platform users", "/admin/users", "users"), NavItem("account", "Your account", "/admin/account", "account"))))


def _head(title: str, suffix: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{esc(title)} · {esc(suffix)}</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%234f46e5'/%3E%3Cstop offset='1' stop-color='%2306b6d4'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='32' height='32' rx='9' fill='url(%23g)'/%3E%3Cpath d='M5 16h3l2.5-7 4 14 4-11 2.5 7 2-3H27' fill='none' stroke='white' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<link rel="stylesheet" href="{_asset("app.css")}">
<link rel="stylesheet" href="{_asset("review.css")}">
<script src="{_asset("app.js")}" defer></script>
</head>"""


def _asset(name: str) -> str:
    from radreport.api.routes.review_ui import asset_url

    return asset_url(name)


def _brand(sub: str, href: str) -> str:
    return f'<a class="brand" href="{esc(href)}"><span class="brand-mark">{icon("logo")}</span><span><span class="brand-name">radreport</span><span class="brand-sub">{esc(sub)}</span></span></a>'


def _theme_toggle() -> str:
    return f'<button type="button" class="ghost icon-btn" data-theme-toggle aria-label="Switch light or dark theme" title="Switch theme">{icon("moon")}</button>'


def _crumbs(crumbs: Sequence[tuple[str, str | None]]) -> str:
    out: list[str] = []
    for i, (label, href) in enumerate(crumbs):
        last = i == len(crumbs) - 1
        cls = "here" if last else ("crumb-mid" if 0 < i < len(crumbs) - 1 else "")
        piece = f'<span class="{cls}">{esc(label)}</span>' if last or not href else f'<a class="{cls}" href="{esc(href)}">{esc(label)}</a>'
        if i:
            out.append(f'<span class="sep {cls if cls == "crumb-mid" else ""}">/</span>')
        out.append(piece)
    return f'<nav class="crumbs" aria-label="Breadcrumb">{"".join(out)}</nav>'


def _shell(*, title: str, suffix: str, brand_sub: str, home: str, nav: Iterable[tuple[str, Iterable[NavItem]]], active: str, user_name: str, user_role: str, sign_out_action: str, body: str, subtitle: str | None, eyebrow: str | None, actions: str, crumbs: Sequence[tuple[str, str | None]]) -> HTMLResponse:
    groups = "".join(f'<div class="nav-label">{esc(label)}</div>' + "".join(f'<a class="nav-item{" active" if item.key == active else ""}" href="{esc(item.href)}"{" aria-current=page" if item.key == active else ""}>{icon(item.icon)}<span>{esc(item.label)}</span>{f"<span class=count>{item.count}</span>" if item.count is not None else ""}</a>' for item in items) for label, items in nav)
    head_actions = f'<div class="page-actions">{actions}</div>' if actions else ""
    lead = f'<div class="eyebrow">{esc(eyebrow)}</div>' if eyebrow else ""
    sub = f'<p class="subtitle">{subtitle}</p>' if subtitle else ""
    return HTMLResponse(f"""{_head(title, suffix)}
<body>
<div class="shell">
  <aside class="sidebar" id="sidebar" aria-label="Main navigation">
    {_brand(brand_sub, home)}
    <nav>{groups}</nav>
    <div class="sidebar-foot">
      <div class="user-chip"><span class="avatar">{esc(_initials(user_name))}</span><div class="who"><strong>{esc(user_name)}</strong><span>{esc(user_role.replace("_", " "))}</span></div></div>
      <form method="post" action="{esc(sign_out_action)}"><button type="submit" class="ghost sm" style="width:100%;justify-content:flex-start">{icon("logout")}Sign out</button></form>
    </div>
  </aside>
  <div class="scrim" data-close-nav></div>
  <div class="main">
    <header class="topbar">
      <button type="button" class="ghost icon-btn nav-toggle" data-open-nav aria-label="Open navigation" aria-controls="sidebar">{icon("menu")}</button>
      {_crumbs(crumbs or ((title, None),))}
      <div class="topbar-actions">{_theme_toggle()}</div>
    </header>
    <main class="content fade-in">
      <div class="page-head"><div>{lead}<h1>{esc(title)}</h1>{sub}</div>{head_actions}</div>
      {body}
    </main>
  </div>
</div>
</body></html>""")


def admin_page(title: str, body: str, *, admin_name: str, admin_role: str, active: str = "", subtitle: str | None = None, eyebrow: str | None = None, actions: str = "", crumbs: Sequence[tuple[str, str | None]] = ()) -> HTMLResponse:
    """A page inside the admin panel's frame."""
    return _shell(title=title, suffix="radreport admin", brand_sub="Admin panel", home="/admin/labs", nav=ADMIN_NAV, active=active, user_name=admin_name, user_role=admin_role, sign_out_action="/admin/logout", body=body, subtitle=subtitle, eyebrow=eyebrow, actions=actions, crumbs=crumbs)


def lab_nav(roles: Iterable[str]) -> tuple[tuple[str, tuple[NavItem, ...]], ...]:
    """The lab-side navigation, showing only what the signed-in roles can open."""
    held = set(roles)
    work = [NavItem("queue", "Review queue", "/ui/queue", "queue")]
    if held & {"radiologist", "lab_admin"}:
        work.append(NavItem("lexicon", "New terms", "/ui/lexicon", "lexicon"))
    return (("Reporting", tuple(work)),)


def lab_page(title: str, body: str, *, user_name: str, user_role: str, roles: Iterable[str] = (), active: str = "", subtitle: str | None = None, eyebrow: str | None = None, actions: str = "", crumbs: Sequence[tuple[str, str | None]] = ()) -> HTMLResponse:
    """A page inside the lab screens' frame."""
    return _shell(title=title, suffix="radreport", brand_sub="Reporting", home="/ui/queue", nav=lab_nav(roles), active=active, user_name=user_name, user_role=user_role, sign_out_action="/ui/logout", body=body, subtitle=subtitle, eyebrow=eyebrow, actions=actions, crumbs=crumbs)


_HERO_POINTS = (("mic", "Dictation in, structured report out", "Speech is transcribed against each lab's own vocabulary, then filled into its templates with every value tied to the audio that supports it."), ("shield", "Safety checks before anyone signs", "Critical findings, contradictions and system-filled values are flagged first, and a report cannot be signed past them."), ("labs", "Every lab kept apart", "Row-level isolation in the database itself: one lab's patients are unreachable from another lab's session."))


def auth_page(title: str, card_html: str, *, subtitle: str, realm: str) -> HTMLResponse:
    """A sign-in page: the product on one side, the form on the other."""
    points = "".join(f'<div class="point">{icon(i)}<div><strong>{esc(h)}</strong><span>{esc(t)}</span></div></div>' for i, h, t in _HERO_POINTS)
    wave = '<svg class="wave" viewBox="0 0 520 160" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><path d="M0 80h40l20-50 30 100 30-80 25 60 20-30h40l20-60 30 120 30-90 25 50 20-20h40l20-40 30 80 30-60 25 30h45"/></svg>'
    return HTMLResponse(f"""{_head(title, "radreport")}
<body>
<div class="auth">
  <section class="auth-hero">
    {_brand(realm, "#")}
    {wave}
    <div>
      <h2>Radiology reports, dictated once and checked before they leave.</h2>
      <p>radreport turns a radiologist's dictation into a structured, grounded draft and puts the parts that need a human eye in front of one.</p>
      <div class="points">{points}</div>
    </div>
    <div class="foot">Synthetic data only on developer machines · Every action is audited</div>
  </section>
  <section class="auth-panel">
    <div class="auth-card fade-in">
      <div class="spread" style="margin-bottom:18px"><span></span>{_theme_toggle()}</div>
      <h1>{esc(title)}</h1>
      <p class="subtitle">{esc(subtitle)}</p>
      {card_html}
    </div>
  </section>
</div>
</body></html>""")


# ------------------------------------------------------------- components ---
def flash(error: str | None = None, notice: str | None = None) -> str:
    """The one-shot messages a redirect carries."""
    out = ""
    if error:
        out += f'<div class="err" role="alert">{icon("alert")}<div>{esc(error)}</div></div>'
    if notice:
        out += f'<div class="banner ok" role="status">{icon("check")}<div>{esc(notice)}</div><button type="button" class="ghost sm right" data-dismiss aria-label="Dismiss">{icon("close")}</button></div>'
    return out


def banner(text_html: str, *, tone: str = "info", icon_name: str | None = None) -> str:
    """A standing message; `text_html` is already escaped."""
    glyph = icon_name or {"ok": "check", "alert": "alert", "blocked": "alert", "warn": "alert"}.get(tone, "info")
    return f'<div class="banner {esc(tone)}">{icon(glyph)}<div>{text_html}</div></div>'


def card(body: str, *, title: str | None = None, subtitle: str | None = None, icon_name: str | None = None, actions: str = "", cls: str = "", id: str | None = None) -> str:  # noqa: A002 - an HTML id
    head = ""
    if title:
        glyph = f'<span class="card-icon">{icon(icon_name)}</span>' if icon_name else ""
        sub = f'<div class="sub">{subtitle}</div>' if subtitle else ""
        head = f'<div class="card-head"><div class="card-title">{glyph}<div><h2>{esc(title)}</h2>{sub}</div></div>{f"<div class=row>{actions}</div>" if actions else ""}</div>'
    ident = f' id="{esc(id)}"' if id else ""
    return f'<section class="card {esc(cls)}"{ident}>{head}{body}</section>'


def stat(label: str, value: str, *, hint: str = "", tone: str = "", icon_name: str = "spark") -> str:
    """A headline number; `value` and `hint` are already escaped."""
    return f'<div class="stat {esc(tone)}"><div class="stat-label">{icon(icon_name)}{esc(label)}</div><div class="stat-value">{value}</div><div class="stat-hint">{hint}</div></div>'


def badge(text: str, tone: str = "muted", *, dot: bool = True) -> str:
    return f'<span class="badge {esc(tone)}{"" if dot else " plain"}">{esc(text)}</span>'


#: The badge tone for each status word the product uses.
STATUS_TONES: dict[str, str] = {"active": "ok", "live": "ok", "pilot": "info", "onboarding": "brand", "provisioning": "muted", "suspended": "warn", "offboarded": "muted", "pass": "ok", "warn": "warn", "fail": "danger", "succeeded": "ok", "failed": "danger", "running": "info", "queued": "muted", "dead": "danger", "pending": "warn", "approved": "ok", "rejected": "muted", "applied": "ok", "reverted": "muted", "awaiting_review": "warn"}


def status_badge(status: str) -> str:
    return badge(status.replace("_", " "), STATUS_TONES.get(status, "muted"))


def table(headers: Sequence[str], rows: Iterable[str], *, empty_text: str = "Nothing here yet.", empty_icon: str = "doc", numeric: Sequence[int] = ()) -> str:
    """A scrollable table; each row is a ready `<tr>`."""
    body = "".join(rows)
    heads = "".join(f"<th{' class=num' if i in numeric else ''}>{esc(h)}</th>" for i, h in enumerate(headers))
    if not body:
        body = f'<tr><td colspan="{len(headers)}">{empty(empty_text, icon_name=empty_icon)}</td></tr>'
    return f'<div class="table-wrap"><table><thead><tr>{heads}</tr></thead><tbody>{body}</tbody></table></div>'


def empty(text: str, *, title: str | None = None, icon_name: str = "doc") -> str:
    return f'<div class="empty">{icon(icon_name)}{f"<strong>{esc(title)}</strong>" if title else ""}<div>{esc(text)}</div></div>'


def progress(done: float, total: float) -> str:
    share = 0.0 if total <= 0 else max(0.0, min(1.0, done / total))
    return f'<div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{round(share * 100)}"><span style="width:{share * 100:.1f}%"></span></div>'


def facts(pairs: Iterable[tuple[str, str]]) -> str:
    """A definition list; values are already escaped."""
    return '<dl class="facts">' + "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in pairs) + "</dl>"


def steps(items: Sequence[tuple[str, str]], *, current: int) -> str:
    """A progress strip of named steps; those before `current` are done."""
    return '<div class="steps">' + "".join(f'<div class="step{" done" if i < current else " current" if i == current else ""}"><div class="n">{"&#10003;" if i < current else i + 1}</div><strong>{esc(t)}</strong><span>{esc(d)}</span></div>' for i, (t, d) in enumerate(items)) + "</div>"


def line_chart(points: Sequence[tuple[str, float]], *, anomalies: Iterable[int] = (), height: int = 220, money: bool = True) -> str:
    """An inline SVG area chart of (label, value) points; anomalies are marked in red."""
    if not points:
        return empty("No runs in this period yet.", icon_name="cost")
    width, pad_l, pad_r, pad_t, pad_b = 720, 52, 16, 14, 30
    values = [v for _, v in points]
    top = max(values) or 1.0
    top *= 1.15
    inner_w, inner_h = width - pad_l - pad_r, height - pad_t - pad_b
    step = inner_w / max(len(points) - 1, 1)

    def xy(i: int, v: float) -> tuple[float, float]:
        return pad_l + i * step, pad_t + inner_h - (v / top) * inner_h

    coords = [xy(i, v) for i, v in enumerate(values)]
    line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    area = f"{line} L{coords[-1][0]:.1f},{pad_t + inner_h:.1f} L{coords[0][0]:.1f},{pad_t + inner_h:.1f} Z"
    fmt = (lambda v: f"${v:,.2f}") if money else (lambda v: f"{v:,.0f}")
    grid = "".join(f'<line class="grid-line" x1="{pad_l}" x2="{width - pad_r}" y1="{pad_t + inner_h * f:.1f}" y2="{pad_t + inner_h * f:.1f}"/><text class="axis-label" x="{pad_l - 8}" y="{pad_t + inner_h * f + 4:.1f}" text-anchor="end">{esc(fmt(top * (1 - f)))}</text>' for f in (0.0, 0.25, 0.5, 0.75, 1.0))
    every = max(1, len(points) // 8)
    labels = "".join(f'<text class="axis-label" x="{coords[i][0]:.1f}" y="{height - 8}" text-anchor="middle">{esc(points[i][0])}</text>' for i in range(0, len(points), every))
    flagged = set(anomalies)
    dots = "".join(f'<circle class="dot{" anomaly" if i in flagged else ""}" cx="{x:.1f}" cy="{y:.1f}" r="{5 if i in flagged else 3.2}"><title>{esc(points[i][0])}: {esc(fmt(points[i][1]))}{" — unusually high" if i in flagged else ""}</title></circle>' for i, (x, y) in enumerate(coords))
    return f"""<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Trend chart">
  <defs><linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--brand)" stop-opacity=".28"/><stop offset="1" stop-color="var(--brand)" stop-opacity="0"/></linearGradient></defs>
  {grid}<path class="area" d="{area}"/><path class="line" d="{line}"/>{dots}{labels}
</svg>"""


def bar_list(rows: Sequence[tuple[str, float]], *, money: bool = True) -> str:
    """Horizontal bars, longest first, each labelled with its value."""
    if not rows:
        return empty("No stage has recorded any spend yet.", icon_name="cost")
    top = max(v for _, v in rows) or 1.0
    fmt = (lambda v: f"${v:,.4f}" if v < 1 else f"${v:,.2f}") if money else (lambda v: f"{v:,.0f}")
    return '<div class="bars">' + "".join(f'<div class="bar-row"><span class="nowrap" title="{esc(label)}">{esc(label)}</span><div class="bar"><span style="width:{max(2.0, v / top * 100):.1f}%"></span></div><span class="val">{esc(fmt(v))}</span></div>' for label, v in rows) + "</div>"


def pager(*, page: int, pages: int, total: int, path: str, extra: dict[str, str] | None = None) -> str:
    """Previous / next links under a paged table; nothing when it all fits on one page."""
    if pages <= 1:
        return ""
    from urllib.parse import urlencode

    def href(n: int) -> str:
        return f"{esc(path)}?{esc(urlencode({**(extra or {}), 'page': n}))}"

    prev = f'<a class="btn sm" href="{href(page - 1)}" rel="prev">{icon("back")}Previous</a>' if page > 1 else '<span class="btn sm" aria-disabled="true">Previous</span>'
    nxt = f'<a class="btn sm" href="{href(page + 1)}" rel="next">Next{icon("arrow")}</a>' if page < pages else '<span class="btn sm" aria-disabled="true">Next</span>'
    return f'<div class="spread" style="padding:14px 22px"><span class="meta">Page {page} of {pages} · {total} in all</span><div class="row">{prev}{nxt}</div></div>'
