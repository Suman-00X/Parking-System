"""The review screen itself, rendered on the server rather than as a single-page app.

Order: sign in from a browser (login_page, login_submit, refresh_session, logout_submit) -> render
the queue (queue_screen) -> render one draft with its fields and withdrawn spans (review_screen,
render_field, render_retractions). static_file serves the few assets.
"""

from __future__ import annotations

import hashlib
import html
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from radreport.api.deps import CurrentPrincipal, DbSession, client_ip
from radreport.api.routes.review import _reviewer, _tenant
from radreport.api.ui import auth_page, badge, banner, card, empty, facts, flash, icon, lab_page, stat
from radreport.auth import lab
from radreport.auth.lab import ACCESS_COOKIE, REFRESH_COOKIE, REFRESH_COOKIE_PATH, SignInFailed, TokenInvalid
from radreport.cache.lookups import user_roles
from radreport.core.config import get_settings
from radreport.review import session as review_session
from radreport.review import signing
from radreport.review.rbac import PermissionDenied

router = APIRouter(prefix="/ui", tags=["review-ui"])

_STATIC = Path(__file__).resolve().parent.parent / "static"
#: Where a browser may be sent back to after signing in: a review page, never another site.
_SAFE_NEXT = re.compile(r"/ui/[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*")


def _safe_next(target: str | None) -> str:
    return target if target and _SAFE_NEXT.fullmatch(target) else "/ui/queue"


def _with_tokens(response: Response, pair: lab.TokenPair) -> Response:
    """Put a fresh token pair in httponly cookies."""
    secure = get_settings().environment not in ("local", "test", "development")
    response.set_cookie(ACCESS_COOKIE, pair.access_token, httponly=True, samesite="lax", secure=secure, max_age=pair.access_expires_in, path="/")
    response.set_cookie(REFRESH_COOKIE, pair.refresh_token, httponly=True, samesite="strict", secure=secure, max_age=get_settings().lab_auth.refresh_ttl_days * 86400, path=REFRESH_COOKIE_PATH)
    return response


def _without_tokens(response: Response) -> Response:
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    return response


# ================================================================= sign-in ===
@router.get("/login", response_class=HTMLResponse)
def login_page(error: str | None = None, next: str | None = None) -> HTMLResponse:  # noqa: A002 - the query parameter is called next
    """A lab user's sign-in form."""
    form = f"""{flash(error)}
  <form method="post" action="/ui/login">
    <input type="hidden" name="next" value="{_esc(_safe_next(next))}">
    <label for="lab">Lab</label><input id="lab" name="lab" required autocomplete="organization" pattern="[a-z0-9][a-z0-9-]{{1,62}}" placeholder="your-lab">
    <label for="email">Email</label><input id="email" name="email" type="email" required autocomplete="username" placeholder="you@hospital.org">
    <label for="password">Password</label><input id="password" name="password" type="password" required autocomplete="current-password" placeholder="••••••••••••">
    <div class="actions"><button class="primary" type="submit">Sign in</button></div>
  </form>"""
    return auth_page("Sign in", form, subtitle="Radiologists, transcriptionists and lab staff sign in with their lab's short name.", realm="Reporting")


@router.post("/login")
def login_submit(request: Request, lab_slug: Annotated[str, Form(alias="lab")], email: Annotated[str, Form()], password: Annotated[str, Form()], next: Annotated[str | None, Form()] = None) -> Response:  # noqa: A002
    """Sign in and keep the tokens in httponly cookies."""
    try:
        pair = lab.sign_in_to_lab(lab_slug=lab_slug, email=email, password=password, user_agent=request.headers.get("user-agent"), ip_address=client_ip(request))
    except SignInFailed:
        return RedirectResponse("/ui/login?" + urlencode({"error": "Invalid lab, email or password", "next": _safe_next(next)}), status_code=status.HTTP_303_SEE_OTHER)
    return _with_tokens(RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER), pair)


@router.get("/refresh")
def refresh_session(request: Request, next: str | None = None, radreport_lab_refresh: Annotated[str | None, Cookie()] = None) -> Response:  # noqa: A002
    """Swap an expired access cookie for a fresh pair, then go back; or to sign-in if that fails."""
    target = _safe_next(next)
    if radreport_lab_refresh:
        try:
            pair = lab.refresh_in_lab(raw=radreport_lab_refresh, user_agent=request.headers.get("user-agent"), ip_address=client_ip(request))
            return _with_tokens(RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER), pair)
        except TokenInvalid:
            pass
    return _without_tokens(RedirectResponse("/ui/login?" + urlencode({"next": target}), status_code=status.HTTP_303_SEE_OTHER))


@router.post("/logout")
def logout_submit(radreport_lab_refresh: Annotated[str | None, Cookie()] = None) -> Response:
    """End this browser's sign-in."""
    if radreport_lab_refresh:
        lab.logout_in_lab(raw=radreport_lab_refresh)
    return _without_tokens(RedirectResponse("/ui/login", status_code=status.HTTP_303_SEE_OTHER))


@router.get("/static/{name}")
def static_file(name: str, request: Request, v: str | None = None) -> Response:
    """Serve the stylesheets and scripts. No bundler, no build step.

    A request carrying the file's current version (`?v=`, which every page's link does) may be cached
    for a year by browsers and a CDN: a new release changes the version, so the URL changes with it.
    """
    if name not in STATIC_ASSETS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown asset")
    body, version = _asset(name)
    etag = f'"{version}"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=31536000, immutable" if v == version else "public, max-age=300"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(body, media_type="text/javascript" if name.endswith(".js") else "text/css", headers=headers)


STATIC_ASSETS = frozenset({"review.js", "review.css", "app.js", "app.css"})


@lru_cache(maxsize=8)
def _asset(name: str) -> tuple[str, str]:
    """The file's text and a short hash of it, read once per process."""
    body = (_STATIC / name).read_text(encoding="utf-8")
    return body, hashlib.sha256(body.encode()).hexdigest()[:12]


def asset_url(name: str) -> str:
    """The versioned URL a page links to."""
    return f"/ui/static/{name}?v={_asset(name)[1]}"


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def render_field(field: review_session.FieldView) -> str:
    """One field row, with every distinction visible."""
    classes = ["field"]
    if field.is_critical:
        classes.append("critical")
    elif field.is_flagged:
        classes.append("flagged")

    tags: list[str] = []
    if field.is_critical:
        tags.append('<span class="tag critical">critical · never auto-filled</span>')
    if field.system_asserted:
        # the single most important visual distinction.
        tags.append(f'<span class="tag asserted">system-asserted · {_esc(field.fill_source)}</span>')
    if not field.is_grounded:
        tags.append('<span class="tag ungrounded">ungrounded · does not render</span>')
    for reason in field.flag_reasons:
        tags.append(f'<span class="tag flag">{_esc(reason)}</span>')

    value = field.value_text or field.value_enum or ""
    if not value and field.value_numeric is not None:
        value = f"{field.value_numeric:g} {field.value_unit or ''}".strip()

    provenance = ""
    if field.provenance:
        spans = "".join(f'<span class="quote" role="button" tabindex="0" data-audio-start="{int(p["audio_start_ms"])}" data-audio-end="{int(p["audio_end_ms"])}">▶ listen {int(p["audio_start_ms"]) / 1000:.1f}s</span>' for p in field.provenance)
        provenance = f'<div class="provenance">{spans}</div>'
    else:
        provenance = '<div class="provenance">no cited audio</div>'

    default_normal = ""
    if field.default_normal_text:
        # The phrase itself, not merely that a default was applied.
        default_normal = f'<div class="default-normal">filled from the template default: &ldquo;{_esc(field.default_normal_text)}&rdquo;</div>'

    return f"""
    <div class="{" ".join(classes)}" data-field-value-id="{field.field_value_id}">
      <div class="field-head">
        <span class="label">{_esc(field.display_label)}</span>
        <span class="field-section">{_esc(field.section)}</span>
        {"".join(tags)}
      </div>
      <input class="field-input" value="{_esc(value)}"
             data-original="{_esc(value)}" aria-label="{_esc(field.display_label)}">
      {default_normal}
      {provenance}
    </div>"""


def render_retractions(view: review_session.DraftView) -> str:
    """Struck-through retractions with the override beside them."""
    if not view.retractions:
        return ""

    rows = "".join(f'<div><span class="retracted" data-audio-start="{r.audio_start_ms}" data-audio-end="{r.audio_end_ms}">{_esc(r.text)}</span> <span class="override">&rarr; {_esc(r.superseded_by_text or "corrected")}</span></div>' for r in view.retractions)
    return f'<div class="banner blocked">{icon("alert")}<div><strong>{len(view.retractions)} self-correction(s)</strong> — the struck-through text was retracted by the radiologist and does not support any field.{rows}</div></div>'


def _who(session: DbSession, principal: CurrentPrincipal) -> tuple[str, tuple[str, ...]]:
    """The signed-in person's name and roles, for the page frame."""
    assert principal.tenant_id is not None
    user = user_roles(session, principal.tenant_id, principal.id)
    return (user.display_name if user else "Lab user"), (user.roles if user else ())


@router.get("/drafts/{draft_id}", response_class=HTMLResponse)
def review_screen(draft_id: uuid.UUID, session: DbSession, principal: CurrentPrincipal) -> HTMLResponse:
    """The review screen for one draft."""
    reviewer = _reviewer(session, principal)
    tenant_id = _tenant(principal)
    try:
        view = review_session.open_draft(session, tenant_id=tenant_id, draft_id=draft_id, reviewer=reviewer)
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    checks = signing.preflight(session, tenant_id=tenant_id, draft_id=draft_id)
    name, roles = _who(session, principal)

    banners: list[str] = []
    if checks.unacknowledged_alerts:
        banners.append(banner(f"<strong>{len(checks.unacknowledged_alerts)} unacknowledged critical finding.</strong> Acknowledge before signing.", tone="alert"))
    if checks.blocking_findings:
        banners.append(banner(f"This draft contradicts its source: {_esc(', '.join(checks.blocking_findings))}. <strong>Signing is blocked.</strong>", tone="blocked"))
    asserted = view.system_asserted_fields
    if asserted:
        banners.append(banner(f"<strong>{len(asserted)} field(s) were filled by the system</strong> rather than dictated. Check each one.", tone="blocked"))

    can_sign = reviewer.can("sign_report") and checks.may_sign
    sign_note = "" if reviewer.can("sign_report") else " (an assistant may revise; a radiologist signs)"
    flagged = len([f for f in view.fields if f.is_flagged])
    critical = len([f for f in view.fields if f.is_critical])

    fields_html = "".join(render_field(f) for f in view.fields) or empty("This draft has no fields to review.", icon_name="doc")
    confidence_tone = "ok" if view.overall_confidence >= 0.85 else "warn" if view.overall_confidence >= 0.6 else "danger"
    aside = f"""<aside class="review-aside">
      {card(facts((("Reviewer", _esc(reviewer.display_role)), ("Confidence", badge(f"{view.overall_confidence:.2f}", confidence_tone)), ("Flagged", f"{flagged} of {len(view.fields)} fields"), ("Critical", str(critical)), ("Active time", '<span id="active-seconds" class="timer">0</span>s'))), title="This draft", icon_name="doc")}
      {card('<p class="meta">Click <span class="quote">▶ listen</span> on any field to hear the audio it came from. Edits are saved as a revision; signing files the report.</p><audio id="dictation" src="/review/drafts/' + str(draft_id) + '/audio" preload="none" controls style="width:100%"></audio>', title="Dictation", icon_name="mic")}
    </aside>"""
    body = f"""{"".join(banners)}
  {render_retractions(view)}
  <div class="review-layout">
    <div>
      <form id="review-form">{fields_html}</form>
      <div class="review-actions">
        <button type="button" id="save" class="primary">{icon("check")}Save revision</button>
        <button type="button" id="sign" {"" if can_sign else "disabled"}>{icon("shield")}Sign{_esc(sign_note)}</button>
        <button type="button" id="useless" class="ghost">This draft was useless</button>
        <span id="status" class="meta" role="status"></span>
      </div>
    </div>
    {aside}
  </div>
<script type="module">
import {{ FocusTimer, wireClickToListen, collectEdits }} from "{asset_url("review.js")}";
const timer = new FocusTimer();
wireClickToListen(document.getElementById("dictation"));
const form = document.getElementById("review-form");
const statusLine = document.getElementById("status");
const draftId = {str(draft_id)!r};
form.addEventListener("input", (e) => {{ if (e.target.matches(".field-input")) e.target.classList.toggle("changed", e.target.value !== e.target.dataset.original); }});

async function post(url, body) {{
  // No body at all when there is nothing to send: a route that takes none refuses even "{{}}".
  const init = body === undefined ? {{ method: "POST" }} : {{ method: "POST", headers: {{ "content-type": "application/json" }}, body: JSON.stringify(body) }};
  const res = await fetch(url, init);
  if (res.status === 401) {{ location.href = "/ui/refresh?next=" + encodeURIComponent(location.pathname); return res; }}
  if (!res.ok) statusLine.textContent = (await res.text()).slice(0, 300);
  return res;
}}

document.getElementById("save").onclick = async () => {{
  statusLine.textContent = "Saving…";
  const res = await post(`/review/drafts/${{draftId}}/revisions`, {{
    edits: collectEdits(form),
    rendered_text: document.querySelector("#review-form").innerText,
    // Focus time and wall clock together: the server clamps one by the other.
    active_edit_seconds: timer.activeSeconds(),
    wall_clock_seconds: timer.wallClockSeconds(),
  }});
  if (res.ok) location.reload();
}};
document.getElementById("sign").onclick = async () => {{
  const res = await post(`/review/drafts/${{draftId}}/sign`);
  if (res.ok) location.href = "/ui/queue";
}};
document.getElementById("useless").onclick = async () => {{
  const reason = window.prompt("What was wrong with it?") || null;
  const res = await post(`/review/drafts/${{draftId}}/usefulness`, {{ was_useless: true, reason }});
  if (res.ok) statusLine.textContent = "Thanks — recorded.";
}};
</script>"""
    return lab_page("Report review", body, user_name=name, user_role=reviewer.display_role, roles=roles, active="queue", eyebrow=f"Draft {str(draft_id)[:8]}", crumbs=(("Review queue", "/ui/queue"), (f"Draft {str(draft_id)[:8]}", None)), actions=f'<a class="btn ghost" href="/ui/queue">{icon("back")}Back to queue</a>')


@router.get("/queue", response_class=HTMLResponse)
def queue_screen(session: DbSession, principal: CurrentPrincipal) -> HTMLResponse:
    """The work list: priority, then alerts, then flagged count."""
    from radreport.review import queue as review_queue

    reviewer = _reviewer(session, principal)
    try:
        items = review_queue.build_queue(session, tenant_id=_tenant(principal), reviewer=reviewer)
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    name, roles = _who(session, principal)

    def _row(i) -> str:
        klass = "critical" if i.has_critical_alert else "flagged" if i.flagged_field_count else ""
        alert_tag = badge("critical finding", "danger") if i.has_critical_alert else ""
        flag_tag = badge(f"{i.flagged_field_count} flagged", "warn") if i.flagged_field_count else ""
        radiologist = badge("radiologist only", "info", dot=False) if i.requires_radiologist else ""
        glyph = icon("alert" if i.has_critical_alert else "flag" if i.flagged_field_count else "doc")
        return f"""<div class="card queue-item {klass}" style="padding:16px 18px">
      <span class="glyph">{glyph}</span>
      <div style="min-width:0">
        <a class="label" href="/ui/drafts/{i.draft_id}"><strong>{_esc(i.template_display_name)}</strong></a>
        <div class="chips">{badge(i.priority, "brand", dot=False)}{alert_tag}{flag_tag}{radiologist}{badge(f"confidence {i.confidence:.2f}", "muted", dot=False)}</div>
      </div>
      <div class="row" style="justify-content:flex-end"><span class="wait">{icon("clock")} {i.waiting_minutes} min</span><a class="btn sm primary" href="/ui/drafts/{i.draft_id}">Review {icon("arrow")}</a></div>
    </div>"""

    criticals = sum(1 for i in items if i.has_critical_alert)
    flagged = sum(1 for i in items if i.flagged_field_count)
    oldest = max((i.waiting_minutes for i in items), default=0)
    kpis = f"""<div class="grid cols-4" style="margin-bottom:18px">
      {stat("Waiting", str(len(items)), hint="drafts you can complete", icon_name="queue")}
      {stat("Critical findings", str(criticals), hint="acknowledge before signing", tone="danger" if criticals else "ok", icon_name="alert")}
      {stat("With flags", str(flagged), hint="fields needing a closer look", tone="warn" if flagged else "", icon_name="flag")}
      {stat("Oldest", f"{oldest} min", hint="time since the draft was ready", icon_name="clock")}
    </div>"""
    rows = "".join(_row(i) for i in items)
    listing = f'<div class="queue-list">{rows}</div>' if items else card(empty("New drafts appear here as soon as the pipeline finishes them.", title="Nothing waiting", icon_name="check"))
    return lab_page("Review queue", kpis + listing, user_name=name, user_role=reviewer.display_role, roles=roles, active="queue", eyebrow="Reporting", subtitle="Ordered by priority, then critical findings, then flagged fields.")


def _highlight(sentence: str, term: str) -> str:
    """The sentence, escaped, with the term marked."""
    escaped = _esc(sentence)
    pattern = re.compile(re.escape(_esc(term)), re.IGNORECASE)
    return pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped, count=1)


@router.get("/lexicon", response_class=HTMLResponse)
def lexicon_screen(session: DbSession, principal: CurrentPrincipal) -> HTMLResponse:
    """Terms radiologists keep using that the lab's lexicon lacks; a radiologist approves them into a new version."""
    from radreport.api.routes.ga import _require_lab_role
    from radreport.core.types import UserRole
    from radreport.knowledge import variant_review
    from radreport.onboarding import term_watch

    _require_lab_role(session, principal, UserRole.RADIOLOGIST, UserRole.LAB_ADMIN)
    name, roles = _who(session, principal)
    can_decide = "radiologist" in roles
    assert principal.tenant_id is not None
    rows = list(session.execute(term_watch.pending(session, principal.tenant_id).limit(100)).scalars())
    body_rows = "".join(
        f"""<tr><td>{f'<input type="checkbox" name="id" value="{r.id}" aria-label="Select {_esc(r.surface_text)}">' if can_decide else ""}</td>
        <td><strong>{_esc(r.surface_text)}</strong><div class="cell-sub">{_esc(r.term_type.replace("_", " "))}</div></td>
        <td class="num"><strong>{r.frequency}</strong></td>
        <td>{"".join(f'<div class="cell-sub">{_highlight(c, r.surface_text)}</div>' for c in (r.contexts or [])[:2]) or '<span class="muted">—</span>'}</td>
        <td class="nowrap cell-sub">{_esc(r.last_seen_at.strftime("%d %b %Y"))}</td></tr>"""
        for r in rows
    )
    controls = """<div class="row"><button type="button" class="primary" data-decide="approve">Approve selected</button><button type="button" data-decide="reject">Not a term</button><button type="button" class="ghost" id="scan">Scan new edits</button><span id="status" class="meta" role="status"></span></div>""" if can_decide else '<p class="meta">A radiologist approves new terms; you can see what is waiting.</p><button type="button" class="ghost" id="scan">Scan new edits</button> <span id="status" class="meta" role="status"></span>'
    total = sum(r.frequency for r in rows)
    kpis = f"""<div class="grid cols-3" style="margin-bottom:18px">
      {stat("Waiting", str(len(rows)), hint="terms not in your lexicon yet", icon_name="lexicon")}
      {stat("Uses", str(total), hint="times they were typed into reports", icon_name="doc")}
      {stat("Most used", _esc(rows[0].surface_text) if rows else "—", hint=f"{rows[0].frequency} uses" if rows else "nothing waiting", icon_name="spark")}
    </div>"""
    listing = card(
        f"""<div class="card-head" style="padding:0;margin-bottom:12px">{controls}</div>
        <div class="table-wrap"><table><thead><tr><th></th><th>Term</th><th class="num">Uses</th><th>Where it appeared</th><th>Last seen</th></tr></thead>
        <tbody>{body_rows or f'<tr><td colspan="5">{empty("Approved terms join your lexicon, so speech recognition and matching know them from then on.", title="No new terms", icon_name="check")}</td></tr>'}</tbody></table></div>""",
        title="Terms your radiologists use that the lexicon does not know",
        subtitle="Collected from report edits every night. Approving makes a new version of the lab's lexicon; earlier versions are kept.",
        icon_name="lexicon",
    )
    unsure = list(session.execute(variant_review.queue(principal.tenant_id, "pending").limit(30)).all())
    let_through = list(session.execute(variant_review.queue(principal.tenant_id, "auto_approved").limit(12)).all())

    def _pair(variant: Any, canonical: str, term_type: str, *, spot_check: bool) -> str:
        confidence = float(variant.confidence or 0)
        if spot_check:
            answers = f'<button type="button" class="ghost" data-variant="{variant.id}" data-answer="different">Not the same</button>' if can_decide else ""
        else:
            answers = (f'<button type="button" class="primary" data-variant="{variant.id}" data-answer="same">Yes, same</button><button type="button" data-variant="{variant.id}" data-answer="different">No, different</button><button type="button" class="ghost" data-variant="{variant.id}" data-answer="unsure">Unsure</button>') if can_decide else ""
        return f"""<div class="pair" id="v-{variant.id}">
          <div class="pair-terms"><span class="pair-heard">{_esc(variant.surface_text)}</span><span class="pair-arrow" aria-hidden="true">↔</span><span class="pair-term">{_esc(canonical)}</span></div>
          <div class="pair-meta"><span class="meter" style="--v:{confidence * 100:.0f}%" title="Match confidence"><i></i></span><span class="cell-sub">{confidence:.0%} sure · heard {variant.observed_count}× · {_esc(term_type.replace("_", " "))}</span></div>
          <div class="pair-actions">{answers}</div></div>"""

    review_card = card("".join(_pair(*row, spot_check=False) for row in unsure) or empty("When the system is not sure a heard phrase means a term, it asks here instead of guessing.", title="Nothing to confirm", icon_name="check"), title="Is this the same term?", subtitle="Sound-alike matches between what was dictated and your lexicon. Only answered or confident matches are used to correct transcripts.", icon_name="mic")
    spot_card = card("".join(_pair(*row, spot_check=True) for row in let_through) or empty("Matches the system approves on its own appear here for a spot check.", title="No automatic approvals yet", icon_name="spark"), title="Approved automatically", subtitle="Confident matches used without asking. Mark any that are wrong; overrides tune the thresholds.", icon_name="shield")
    variants_block = f'<div class="grid cols-2" style="margin-top:18px">{review_card}{spot_card}</div>'
    script = """<script>
const statusLine = document.getElementById("status");
document.querySelectorAll("[data-variant]").forEach((button) => button.addEventListener("click", async () => {
  const res = await fetch(`/lexicon/variants/${button.dataset.variant}/decide`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ answer: button.dataset.answer }) });
  if (res.status === 401) { location.href = "/ui/refresh?next=" + encodeURIComponent(location.pathname); return; }
  const row = document.getElementById(`v-${button.dataset.variant}`);
  if (!res.ok) { row.querySelector(".pair-actions").textContent = "Could not save — try again."; return; }
  if (button.dataset.answer === "unsure") { row.classList.add("is-unsure"); return; }
  row.classList.add("is-done"); setTimeout(() => row.remove(), 350);
}));
async function send(url, body) {
  const res = await fetch(url, { method: "POST", headers: body ? { "content-type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  if (res.status === 401) { location.href = "/ui/refresh?next=" + encodeURIComponent(location.pathname); return null; }
  if (!res.ok) { statusLine.textContent = (await res.text()).slice(0, 200); return null; }
  return res.json();
}
document.querySelectorAll("[data-decide]").forEach((button) => button.addEventListener("click", async () => {
  const ids = [...document.querySelectorAll("input[name=id]:checked")].map((box) => box.value);
  if (!ids.length) { statusLine.textContent = "Select at least one term."; return; }
  const done = await send(`/lexicon/candidates/${button.dataset.decide}`, { ids });
  if (done) location.reload();
}));
document.getElementById("scan").addEventListener("click", async () => {
  statusLine.textContent = "Scanning…";
  const done = await send("/lexicon/scan");
  if (done) { statusLine.textContent = `${done.unknown_terms} new term(s) in ${done.edit_events} edit(s).`; setTimeout(() => location.reload(), 900); }
});
</script>"""
    return lab_page("New terms", kpis + listing + variants_block + script, user_name=name, user_role="Radiologist" if can_decide else "Lab admin", roles=roles, active="lexicon", eyebrow="Lexicon", subtitle="Grow the lab's vocabulary from how its radiologists actually write.")
