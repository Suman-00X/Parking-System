"""The public pages: what the product does, its HTTP API, and an overview of the project for recruiters.

Order: the features page renders FEATURES.md (features) -> the API page renders API.md beside a
table of contents and points at the live OpenAPI docs where the environment serves them
(api_docs) -> the recruiter page summarises what was built, with numbers counted from the code
itself and the read-only demo sign-ins (recruiter). Nothing here needs a sign-in or reads lab data.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from radreport.api.markdown import render
from radreport.api.ui import demo_accounts, demo_credentials, esc, icon, public_page

router = APIRouter(tags=["public"])

_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE = Path(__file__).resolve().parents[2]


def _doc(name: str) -> str | None:
    path = _ROOT / name
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _site_link(href: str) -> str | None:
    """Repository links in the docs, pointed at the matching page, or dropped when there is none."""
    target, _, anchor = href.partition("#")
    pages = {"FEATURES.md": "/features", "API.md": "/api-docs", "": ""}
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


@router.get("/features", response_class=HTMLResponse)
def features() -> HTMLResponse:
    """What radreport does, for anyone."""
    rendered = _rendered("FEATURES.md")
    if rendered is None:
        return _missing("Features", "features")
    html, toc = rendered
    body = f"""<div class="doc-layout">{_toc(toc, deepest=2)}<article class="md doc-features">{html}</article></div>"""
    return public_page("Features", body, active="features", description="What radreport does: dictation in, a structured, checked report out.")


@router.get("/api-docs", response_class=HTMLResponse)
def api_docs() -> HTMLResponse:
    """The HTTP API, in the order the product uses it."""
    from radreport.core.config import get_settings

    rendered = _rendered("API.md")
    if rendered is None:
        return _missing("API docs", "api")
    html, toc = rendered
    live = get_settings().environment in ("local", "test", "development")
    banner = f"""<div class="banner info doc-banner">{icon("info")}<div>{'The live, interactive OpenAPI reference is at <a href="/docs">/docs</a> (and <a href="/openapi.json">/openapi.json</a>) on this deployment.' if live else "This page is the full reference. The interactive OpenAPI pages are switched off outside development deployments."} Every route and parameter is also declared in the access policy, which the server enforces on each request.</div></div>"""
    body = f"""<div class="doc-layout">{_toc(toc, deepest=2)}<article class="md doc-api">{banner}{html}</article></div>"""
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
    admin_demo, lab_demo = demo_accounts("admin"), demo_accounts("lab")
    if admin_demo or lab_demo:
        demo = f"""<div class="show-demo">
          <div><h3>{icon("labs")} Admin panel</h3>{demo_credentials("admin") if admin_demo else '<p class="meta">No admin demo account on this deployment.</p>'}<a class="btn primary" href="/admin/login">Open the admin panel</a></div>
          <div><h3>{icon("queue")} Lab screens</h3>{demo_credentials("lab") if lab_demo else '<p class="meta">No lab demo account on this deployment.</p>'}<a class="btn primary" href="/ui/login">Open the lab screens</a></div>
        </div>"""
    else:
        demo = '<p class="meta">No demo sign-ins are configured on this deployment. Set <code>RADREPORT_DEMO_ACCOUNTS</code> to read-only accounts (support for the admin panel, an auditor for a lab) to show them here.</p>'
    body = f"""
    <section class="show-hero">
      <div class="eyebrow">Project overview</div>
      <h1>radreport: radiology reports, dictated once and checked before they leave.</h1>
      <p class="lead">A multi-lab platform that turns a radiologist's dictation into a structured, grounded draft, puts the risky parts in front of a person, and earns the right to skip review only where the evidence says it is safe. Built as a production-shaped system: isolation in the database, every route declared, measured performance work, and a test suite that tries to break it.</p>
      <div class="show-cta"><a class="btn primary" href="#try-it">Try the demo</a><a class="btn" href="/features">Features</a><a class="btn" href="/api-docs">API docs</a></div>
    </section>
    <section class="show-stats" aria-label="Project in numbers">{stat_html}</section>
    <section><h2 class="show-h2">What it demonstrates</h2><div class="show-grid">{built}</div></section>
    <section><h2 class="show-h2">Built with</h2><div class="chips">{stack}</div></section>
    <section id="try-it"><h2 class="show-h2">Try it</h2>
      <p>The demo lab, Sunrise Imaging, holds synthetic data only: templates, a report history, drafts at every stage, signed and graded reports, critical alerts and lexicon suggestions. The accounts below can open every screen and change nothing.</p>
      {demo}
    </section>"""
    return public_page("For recruiters", body, active="recruiter", description="radreport, a multi-lab radiology reporting platform: what was built and how to try it.")
