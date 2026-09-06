"""The review screen itself, rendered on the server rather than as a single-page app.

Order: render the queue (queue_screen) -> render one draft with its fields and withdrawn spans
(review_screen, render_field, render_retractions). static_file serves the few assets.
"""

from __future__ import annotations

import html
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse, Response

from radreport.api.deps import CurrentPrincipal, DbSession
from radreport.api.routes.review import _reviewer, _tenant
from radreport.review import session as review_session
from radreport.review import signing
from radreport.review.rbac import PermissionDenied

router = APIRouter(prefix="/ui", tags=["review-ui"])

_STATIC = Path(__file__).resolve().parent.parent / "static"


@router.get("/static/{name}")
def static_file(name: str) -> Response:
    """Serve the two review-screen assets. No bundler, no build step."""
    if name not in {"review.js", "review.css"}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown asset")
    path = _STATIC / name
    media = "text/javascript" if name.endswith(".js") else "text/css"
    return Response(path.read_text(encoding="utf-8"), media_type=media)


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
        spans = "".join(f'<span class="quote" data-audio-start="{int(p["audio_start_ms"])}" data-audio-end="{int(p["audio_end_ms"])}">▶ listen ({int(p["audio_start_ms"]) / 1000:.1f}s)</span> ' for p in field.provenance)
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
        <span class="section">{_esc(field.section)}</span>
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
    return f'<div class="banner blocked"><strong>{len(view.retractions)} self-correction(s)</strong> — the struck-through text was retracted by the radiologist and does not support any field.{rows}</div>'


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

    banners: list[str] = []
    if checks.unacknowledged_alerts:
        banners.append(f'<div class="banner alert">⚠ {len(checks.unacknowledged_alerts)} unacknowledged critical finding. Acknowledge before signing.</div>')
    if checks.blocking_findings:
        banners.append(f'<div class="banner blocked">This draft contradicts its source: {_esc(", ".join(checks.blocking_findings))}. Signing is blocked.</div>')
    asserted = view.system_asserted_fields
    if asserted:
        banners.append(f'<div class="banner blocked">{len(asserted)} field(s) were filled by the system rather than dictated. Check each one.</div>')

    can_sign = reviewer.can("sign_report") and checks.may_sign
    sign_note = "" if reviewer.can("sign_report") else " (an assistant may revise; a radiologist signs)"

    fields_html = "".join(render_field(f) for f in view.fields)

    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review {_esc(str(draft_id)[:8])}</title>
<link rel="stylesheet" href="/ui/static/review.css">
</head><body><main>
  <h1>Report review</h1>
  <div class="meta">
    {_esc(reviewer.display_role)} · confidence {view.overall_confidence:.2f} ·
    {len([f for f in view.fields if f.is_flagged])} flagged of {len(view.fields)} fields ·
    active <span id="active-seconds" class="timer">0</span>s
  </div>
  {"".join(banners)}
  {render_retractions(view)}
  <audio id="dictation" src="/review/drafts/{draft_id}/audio" preload="none"></audio>
  <form id="review-form">{fields_html}</form>
  <div class="actions">
    <button type="button" id="save" class="primary">Save revision</button>
    <button type="button" id="sign" {"" if can_sign else "disabled"}>Sign{_esc(sign_note)}</button>
    <button type="button" id="useless">This draft was useless</button>
  </div>
<script type="module">
import {{ FocusTimer, wireClickToListen, collectEdits }} from "/ui/static/review.js";
const timer = new FocusTimer();
wireClickToListen(document.getElementById("dictation"));
const form = document.getElementById("review-form");
const draftId = {str(draft_id)!r};

async function post(url, body) {{
  const res = await fetch(url, {{
    method: "POST",
    headers: {{ "content-type": "application/json" }},
    body: JSON.stringify(body),
  }});
  if (!res.ok) alert(await res.text());
  return res;
}}

document.getElementById("save").onclick = async () => {{
  await post(`/review/drafts/${{draftId}}/revisions`, {{
    edits: collectEdits(form),
    rendered_text: document.querySelector("#review-form").innerText,
    // Focus time and wall clock together: the server clamps one by the other.
    active_edit_seconds: timer.activeSeconds(),
    wall_clock_seconds: timer.wallClockSeconds(),
  }});
  location.reload();
}};
document.getElementById("sign").onclick = async () => {{
  const res = await post(`/review/drafts/${{draftId}}/sign`, {{}});
  if (res.ok) location.href = "/ui/queue";
}};
document.getElementById("useless").onclick = async () => {{
  const reason = prompt("What was wrong with it?") || null;
  await post(`/review/drafts/${{draftId}}/usefulness`, {{ was_useless: true, reason }});
}};
</script>
</main></body></html>""")


@router.get("/queue", response_class=HTMLResponse)
def queue_screen(session: DbSession, principal: CurrentPrincipal) -> HTMLResponse:
    """The work list: priority, then alerts, then flagged count."""
    from radreport.review import queue as review_queue

    reviewer = _reviewer(session, principal)
    try:
        items = review_queue.build_queue(session, tenant_id=_tenant(principal), reviewer=reviewer)
    except PermissionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    def _row(i) -> str:
        klass = "critical" if i.has_critical_alert else "flagged" if i.flagged_field_count else ""
        alert_tag = '<span class="tag critical">critical finding</span>' if i.has_critical_alert else ""
        flag_tag = f'<span class="tag flag">{i.flagged_field_count} flagged</span>' if i.flagged_field_count else ""
        return f"""<div class="field {klass}">
      <div class="field-head">
        <a class="label" href="/ui/drafts/{i.draft_id}">{_esc(i.template_display_name)}</a>
        <span class="section">{_esc(i.priority)}</span>
        {alert_tag}{flag_tag}
        <span class="tag ungrounded">{i.waiting_minutes} min</span>
      </div>
    </div>"""

    rows = "".join(_row(i) for i in items)
    empty = '<p class="meta">Nothing waiting.</p>' if not items else ""

    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review queue</title><link rel="stylesheet" href="/ui/static/review.css">
</head><body><main>
  <h1>Review queue</h1>
  <div class="meta">{_esc(reviewer.display_role)} · {len(items)} waiting ·
    ordered by priority, then critical findings, then flagged fields</div>
  {rows}{empty}
</main></body></html>""")
