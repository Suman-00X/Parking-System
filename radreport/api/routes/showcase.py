"""The public pages: what the product does, a way into the demo, its HTTP API, and a tour of the project for recruiters.

Order: FEATURES.md is split at its tab markers; the features page renders the features tab, each
feature with its Reason (features) -> the demo page lists the read-only demo sign-ins (demo) -> the
API page renders API.md beside a table of contents and points at the live OpenAPI docs where the
environment serves them (api_docs) -> the recruiter tour summarises what was built, with numbers
counted from the code itself, then tabs: the demo sign-ins, the screen recordings, the system design
with the architecture diagram, and the crash test's last results (recruiter). Nothing here needs a
sign-in or reads lab data.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, Response

from radreport.api.diagram import ARCHITECTURE_SVG
from radreport.api.markdown import render
from radreport.api.routing import BridgedRoute
from radreport.api.ui import demo_accounts, demo_credentials, esc, icon, public_page

router = APIRouter(tags=["public"], route_class=BridgedRoute)

_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE = Path(__file__).resolve().parents[2]


def _doc(name: str) -> str | None:
    path = _ROOT / name
    return path.read_text(encoding="utf-8") if path.is_file() else None


#: The FEATURES.md tabs shown on the recruiter tour, in order, after its own demo tab; the rest is the features page.
_TOUR_TABS = ("in-action", "hld", "crash-test")
_SPARKLE_TAB = "in-action"


def _site_link(href: str) -> str | None:
    """Repository links in the docs, pointed at the matching page, or dropped when there is none."""
    target, _, anchor = href.partition("#")
    pages = {"FEATURES.md": "/recruiter" if anchor in _TOUR_TABS else "/features", "API.md": "/api-docs", "": ""}
    if target in pages:
        return pages[target] + (f"#{anchor}" if anchor else "")
    return None


def _toc(entries: list[tuple[int, str, str]], *, deepest: int) -> str:
    items = "".join(f'<li class="toc-{level}"><a href="#{anchor}">{esc(text)}</a></li>' for level, anchor, text in entries if level <= deepest)
    return f'<nav class="doc-toc" aria-label="On this page"><div class="doc-toc-title">On this page</div><ul>{items}</ul></nav>'


@lru_cache(maxsize=4)
def _rendered(name: str) -> tuple[str, list[tuple[int, str, str]]] | None:
    text = _doc(name)
    if text is None:
        return None
    out = render(text, link_base=_site_link)
    return out.html, out.toc


def _missing(title: str, active: str) -> HTMLResponse:
    body = f'<section class="doc-hero"><h1>{esc(title)}</h1><p>This deployment was installed without the project documents, so there is nothing to show here.</p></section>'
    return public_page(title, body, active=active, description=title)


_MEDIA_DIR = _ROOT / "docs" / "media"
_MEDIA_NAME = re.compile(r"[a-z0-9-]+\.(gif|mp4)")
_MEDIA = re.compile(r"^<!--\s*media:\s*([a-z0-9-]+)\s*\|\s*(.+?)\s*-->\s*$", re.M)
_TAB = re.compile(r"^<!--\s*tab:\s*([a-z0-9-]+)\s*\|\s*(.+?)\s*-->\s*$", re.M)
_INCLUDE = re.compile(r"^<!--\s*include:\s*(docs/[\w-]+\.md)\s*-->\s*$", re.M)


def _include(match: re.Match[str]) -> str:
    """A generated document pulled into a tab, without its own title; only Markdown under docs/ may be included."""
    text = _doc(match.group(1))
    if text is None:
        return "*Not run on this deployment yet.*"
    return re.sub(r"\A#\s+[^\n]*\n", "", text.lstrip())


def _media_markers(text: str) -> tuple[str, list[str]]:
    """Each media marker swapped for a placeholder the renderer leaves alone, and the figure that replaces it afterwards."""
    figures: list[str] = []

    def swap(match: re.Match[str]) -> str:
        name, caption = match.group(1), match.group(2)
        if not (_MEDIA_DIR / f"{name}.mp4").is_file():
            return ""
        figures.append(f'<figure class="media"><video src="/media/{name}.mp4" autoplay muted loop playsinline preload="metadata" aria-label="{esc(caption)}"></video><figcaption>{esc(caption)}</figcaption></figure>')
        return f"\n\nMEDIAFIGURE{len(figures) - 1}\n\n"

    return _MEDIA.sub(swap, text), figures


def _split_tabs(text: str) -> tuple[str, list[tuple[str, str, str]]]:
    """The text before the first tab marker, then (key, label, body) for each tab."""
    parts = _TAB.split(text)
    return parts[0], [(parts[i], parts[i + 1], parts[i + 2]) for i in range(1, len(parts) - 2, 3)]


@lru_cache(maxsize=1)
def _feature_doc() -> tuple[str, dict[str, tuple[str, str]]] | None:
    """FEATURES.md as the hero above its first tab, then each tab's label and panel content by key."""
    text = _doc("FEATURES.md")
    if text is None:
        return None
    text, figures = _media_markers(_INCLUDE.sub(_include, text))
    intro, tabs = _split_tabs(text)
    hero = render(intro, link_base=_site_link).html
    panels = {}
    for key, label, body in tabs:
        out = render(body, link_base=_site_link, reasons=True)
        out.html = re.sub(r"<p>MEDIAFIGURE(\d+)</p>", lambda m: figures[int(m.group(1))], out.html)
        lead = ARCHITECTURE_SVG if key == "hld" else ""
        panels[key] = (label, f'<div class="doc-layout">{_toc(out.toc, deepest=3)}<article class="md doc-features">{lead}{out.html}</article></div>')
    return hero, panels


def _tabs(tabs: list[tuple[str, str, str]]) -> str:
    """A tab bar a link can open by key, and its panels; the first tab starts open."""
    def button(n: int, key: str, label: str) -> str:
        sparkle = key == _SPARKLE_TAB
        return f'<button type="button" role="tab" id="tab-btn-{key}" aria-controls="tab-{key}" aria-selected="{str(n == 0).lower()}" data-panel="tab-{key}" data-key="{key}"{' class="tab-sparkle"' if sparkle else ""}>{'<span class="sparkle-star" aria-hidden="true">✦</span>' if sparkle else ""}{esc(label)}</button>'

    bar = "".join(button(n, key, label) for n, (key, label, _content) in enumerate(tabs))
    panels = "".join(f'<section id="tab-{key}" role="tabpanel" aria-labelledby="tab-btn-{key}" class="feature-panel"{"" if n == 0 else " hidden"}>{content}</section>' for n, (key, _label, content) in enumerate(tabs))
    return f'<div class="tabs feature-tabs" role="tablist" aria-label="Sections" data-tabs data-hash-tabs>{bar}</div>{panels}'


@router.get("/media/{name}")
def media(name: str) -> Response:
    """A screen recording for the recruiter tour; byte ranges are honoured, which Safari needs to play video."""
    path = _MEDIA_DIR / name
    if not _MEDIA_NAME.fullmatch(name) or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such recording")
    return FileResponse(path, media_type="video/mp4" if name.endswith(".mp4") else "image/gif", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/features", response_class=HTMLResponse)
def features() -> HTMLResponse:
    """What radreport does, feature by feature, each with the reason it exists."""
    doc = _feature_doc()
    if doc is None or "features" not in doc[1]:
        return _missing("Features", "features")
    hero, panels = doc
    body = f'<section class="md doc-features feature-hero">{hero}</section><div class="feature-single">{panels["features"][1]}</div>'
    return public_page("Features", body, active="features", description="What radreport does: dictation in, a structured, checked report out.")


def _demo_section() -> str:
    """The read-only demo sign-ins for the admin panel and the lab screens, or why there are none."""
    admin_demo, lab_demo = demo_accounts("admin"), demo_accounts("lab")
    intro = "<p>The demo lab, Sunrise Imaging, holds synthetic data only: templates, a report history, drafts at every stage, signed and graded reports, critical alerts and lexicon suggestions. The accounts below can open every screen and change nothing.</p>"
    if not (admin_demo or lab_demo):
        return intro + '<p class="meta">No demo sign-ins are configured on this deployment. Set <code>RADREPORT_DEMO_ACCOUNTS</code> to read-only accounts (support for the admin panel, an auditor for a lab) to show them here.</p>'
    return f"""{intro}<div class="show-demo">
      <div><h3>{icon("labs")} Admin panel</h3>{demo_credentials("admin") if admin_demo else '<p class="meta">No admin demo account on this deployment.</p>'}<a class="btn primary" href="/admin/login">Open the admin panel</a></div>
      <div><h3>{icon("queue")} Lab screens</h3>{demo_credentials("lab") if lab_demo else '<p class="meta">No lab demo account on this deployment.</p>'}<a class="btn primary" href="/ui/login">Open the lab screens</a></div>
    </div>"""


@router.get("/demo", response_class=HTMLResponse)
def demo() -> HTMLResponse:
    """The read-only demo sign-ins, one click from every public page."""
    body = f"""<section class="show-hero"><div class="eyebrow">Try the demo</div><h1>Open radreport with a read-only account.</h1></section>
    <section>{_demo_section()}</section>"""
    return public_page("Try the demo", body, active="demo", description="Read-only demo sign-ins for radreport's admin panel and lab screens, on synthetic data.")


@router.get("/api-docs", response_class=HTMLResponse)
def api_docs() -> HTMLResponse:
    """The HTTP API, in the order the product uses it."""
    from radreport.core.config import get_settings

    rendered = _rendered("API.md")
    if rendered is None:
        return _missing("API docs", "api")
    html, toc = rendered
    live = get_settings().environment in ("local", "test", "development")
    banner = f"""<div class="banner info doc-banner">{icon("info")}<div>Every route and parameter is also declared in the access policy, which the server enforces on each request. {'The Swagger tab runs requests against this deployment; routes that need a sign-in answer 401 without one.' if live else "The Swagger tab is available on local and development deployments."}</div></div>"""
    reference = f"""<div class="doc-layout">{_toc(toc, deepest=2)}<article class="md doc-api">{banner}{html}</article></div>"""
    if live:
        swagger = '<p class="swagger-open"><a href="/docs" target="_blank" rel="noopener">Open Swagger in its own tab</a> · <a href="/openapi.json">openapi.json</a></p><iframe class="swagger-frame" src="/docs" title="Swagger UI" loading="lazy"></iframe>'
    else:
        swagger = f"""<div class="banner info doc-banner">{icon("info")}<div>Swagger UI is switched off on this deployment. Run the app locally (<code>make run</code>) and open this tab, or <a href="http://localhost:8000/docs">localhost:8000/docs</a>.</div></div>"""
    body = _tabs([("reference", "Reference", reference), ("swagger", "Swagger", swagger)])
    return public_page("API docs", body, active="api", description="radreport's HTTP API: every route, who may call it, and what it does.")


@lru_cache(maxsize=1)
def project_numbers() -> dict[str, int]:
    """Counted from the code at first request, so the page never states a stale figure."""
    from radreport.db import models  # noqa: F401 - registers every table
    from radreport.db.base import Base

    python = [p for p in _PACKAGE.rglob("*.py") if "__pycache__" not in p.parts]
    policy = (_PACKAGE / "api" / "access_policy.xml").read_text(encoding="utf-8")
    tests_dir = _ROOT / "tests"
    tests = sum(len(re.findall(r"^\s*(?:async\s+)?def test_", p.read_text(encoding="utf-8"), re.M)) for p in tests_dir.rglob("test_*.py")) if tests_dir.is_dir() else 0
    migrations = [p for p in (_PACKAGE / "db" / "migrations" / "versions").glob("[0-9]*.py")]
    return {"routes": policy.count("<route id="), "tables": len(Base.metadata.tables), "migrations": len(migrations), "python_files": len(python), "python_lines": sum(p.read_text(encoding="utf-8").count("\n") for p in python), "tests": tests}


_BUILT: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("mic", "A dictation-to-report pipeline", "A recording goes through a fifteen-stage graph: speech recognition biased toward the lab's vocabulary, sound-alike term resolution with a margin guard, study-code detection, self-correction repair, critical-finding alerts, template routing, and grounding of every value in the audio that supports it. Low-confidence and critical fields reach the radiologist first.", ("Per-stage traces and costs", "Deterministic stages testable without a model", "Critical alerts block signing until acknowledged")),
    ("shield", "Isolation and access control", "Every lab's rows are walled off by Postgres row-level security, forced on the table owner and tested by suites that try to read across labs. Every route, role, parameter, rate limit and body size is declared in one access policy the server enforces before any handler runs.", ("RLS bound per transaction", "Unknown or malformed input is a 400", "Append-only audit log")),
    ("database", "Scale work, measured", "Query instrumentation per request, N+1 removal and batched writes, PgBouncer in transaction mode, a deadlock found and fixed under a 500-user load test, partitioned tables kept ahead by a scheduler, read-replica routing with read-your-writes, and labs sharded on a consistent-hash ring.", ("500 concurrent users at 99.99% success", "A 15-stage run in 8 statements, down from 37", "5,000-row imports in 0.18 s")),
    ("jobs", "Reliable background work", "Uploads queue a pipeline job claimed with SKIP LOCKED, with visibility timeouts and dead-lettering. Domain events go through a transactional outbox to Postgres or Kafka consumers, exactly once per consumer, even across a crash between commit and publish.", ("Job queue and periodic scheduler", "Outbox relay with per-consumer dedupe", "Redis or in-memory shared cache")),
    ("lexicon", "A vocabulary that learns", "Each lab's lexicon grows from its own use: shorthand sheets, curated synonyms and RadLex lookups, new terms collected from report edits for radiologists to approve, sound-alike matches auto-approved only above a confidence threshold, and Hindi, French and Spanish terms mapped to English.", ("A/B-testable thresholds with override tracking", "A grounded fallback model for messy templates", "Training export only with lab consent")),
    ("spark", "Autonomy that is earned", "Routine report classes can skip review only after a measured, non-inferior error rate against the lab's own baseline. A small sample is always still graded, and a CUSUM monitor withdraws the permission automatically when quality slips.", ("G0–G4 grading of signed reports", "Bayesian non-inferiority evidence", "Instant, mechanical revocation")),
)
_STACK = ("Python 3.13", "FastAPI", "SQLAlchemy 2", "PostgreSQL 16 + pgvector", "Row-level security", "Alembic", "PgBouncer", "Redis", "Kafka / Redpanda", "S3", "Claude and local models", "Whisper", "HL7 v2 and FHIR R4", "pytest")


@router.get("/recruiter", response_class=HTMLResponse)
def recruiter() -> HTMLResponse:
    """What this project is and what it demonstrates, with a way in."""
    n = project_numbers()
    stats = (("routes", f"{n['routes']}", "HTTP routes, each declared in the access policy"), ("tests", f"{n['tests']:,}", "test functions, including cross-lab leak tests"), ("tables", f"{n['tables']}", "database tables under row-level security"), ("migrations", f"{n['migrations']}", "guarded schema migrations"), ("lines", f"{n['python_lines'] / 1000:.0f}k", f"lines of Python in {n['python_files']} files"), ("load", "500", "concurrent users in the load test, 99.99% served"))
    stat_html = "".join(f'<div class="show-stat"><strong>{esc(value)}</strong><span>{esc(label)}</span></div>' for _key, value, label in stats)
    built = "".join(
        f"""<article class="show-card">
        <div class="show-card-icon">{icon(glyph)}</div><h3>{esc(title)}</h3><p>{esc(text)}</p>
        <ul>{"".join(f"<li>{esc(point)}</li>" for point in points)}</ul></article>"""
        for glyph, title, text, points in _BUILT
    )
    stack = "".join(f'<span class="chip">{esc(item)}</span>' for item in _STACK)
    body = f"""
    <section class="show-hero">
      <div class="eyebrow">Project overview</div>
      <h1>radreport: radiology reports, dictated once and checked before they leave.</h1>
      <p class="lead">A multi-lab platform that turns a radiologist's dictation into a structured, grounded draft, puts the risky parts in front of a person, and earns the right to skip review only where the evidence says it is safe. Built as a production-shaped system: isolation in the database, every route declared, measured performance work, and a test suite that tries to break it.</p>
      <div class="show-cta"><a class="btn primary" href="#demo">Try the demo</a><a class="btn" href="#in-action">For Recruiters</a><a class="btn" href="/features">Features</a><a class="btn" href="/api-docs">API docs</a></div>
    </section>
    <section class="show-stats" aria-label="Project in numbers">{stat_html}</section>"""
    doc = _feature_doc()
    panels = doc[1] if doc else {}
    design = f"""<section><h2 class="show-h2">What it demonstrates</h2><div class="show-grid">{built}</div></section>
    <section><h2 class="show-h2">Built with</h2><div class="chips">{stack}</div></section>"""
    tabs = [("demo", "Try the demo", f'<div class="tour-demo">{_demo_section()}</div>')]
    for key in _TOUR_TABS:
        if key in panels:
            label, content = panels[key]
            tabs.append((key, label, design + content if key == "hld" else content))
    body += _tabs(tabs)
    return public_page("Recruiter tour", body, active="recruiter", description="radreport, a multi-lab radiology reporting platform: how to try it, recordings of it running, its system design and a crash test.")
