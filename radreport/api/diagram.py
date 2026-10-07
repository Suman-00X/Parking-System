"""The architecture diagram on the features page's system-design tab, drawn as inline SVG in the page's own colour tokens.

Defines: ARCHITECTURE_SVG, built once from the boxes and arrows below by _box and _arrow; it
follows the page's light or dark theme because every colour is a CSS variable.
"""

from __future__ import annotations

import html

_BOXES: tuple[tuple[str, int, int, int, int, str, str, str], ...] = (
    # key, x, y, width, height, title, subtitle, tone
    ("people", 20, 40, 170, 70, "People", "radiologists, staff, admins", "edge"),
    ("cdn", 225, 40, 160, 70, "CDN", "static files only", "edge"),
    ("web", 420, 20, 240, 110, "Web app × N", "FastAPI · access policy|rate limits · request cache|/health · /ready · /metrics", "core"),
    ("primary", 700, 20, 210, 56, "Postgres primary", "row-level security · partitions", "store"),
    ("shards", 700, 88, 210, 56, "Shards", "labs on a consistent-hash ring", "store"),
    ("replica", 700, 156, 210, 56, "Read replica", "dashboards, lists, exports", "store"),
    ("redis", 700, 224, 210, 56, "Redis", "shared cache and sessions", "store"),
    ("s3", 700, 292, 210, 56, "Object storage", "audio, encrypted, signed links", "store"),
    ("prom", 940, 20, 150, 56, "Prometheus", "→ Grafana dashboards", "obs"),
    ("otel", 940, 88, 150, 56, "OpenTelemetry", "request traces", "obs"),
    ("sentry", 940, 156, 150, 56, "Sentry", "scrubbed error reports", "obs"),
    ("k6", 940, 224, 150, 56, "k6", "scheduled synthetic load", "obs"),
    ("queue", 420, 180, 240, 60, "Job queue", "Postgres · SKIP LOCKED · leases", "core"),
    ("workers", 420, 280, 240, 60, "Workers × N", "heartbeats · retries · dead letters", "core"),
    ("pipeline", 420, 380, 240, 70, "17-step report pipeline", "grounding · urgent alerts|confidence · routing", "core"),
    ("ai", 150, 380, 230, 70, "AI and speech providers", "circuit breaker · backoff|per-provider caps", "edge"),
    ("outbox", 20, 500, 180, 60, "Transactional outbox", "committed with the change", "core"),
    ("relay", 235, 500, 145, 60, "Relay", "at least once", "core"),
    ("bus", 420, 500, 240, 60, "Event bus", "Kafka (Redpanda) or Postgres", "core"),
    ("consumers", 700, 500, 390, 60, "Consumers, each applied exactly once", "HL7 v2 / FHIR export · alerts · metering · analytics", "store"),
)

_ARROWS: tuple[tuple[str, bool] | tuple[str, bool, bool], ...] = (
    # SVG path data, dashed, and (when given) whether it ends in an arrowhead
    ("M190 75 H225", False),
    ("M385 75 H420", False),
    ("M660 48 H700", False),
    ("M680 48 V320 H700", False),
    ("M680 116 H700", False),
    ("M680 184 H700", False),
    ("M680 252 H700", False),
    ("M540 130 V180", False),
    ("M540 240 V280", False),
    ("M540 340 V380", False),
    ("M420 415 H380", False),
    ("M480 450 V475 H110 V500", False),
    ("M200 530 H235", False),
    ("M380 530 H420", False),
    ("M660 530 H700", False),
    ("M600 20 V8 H925 V252", True, False),
    ("M925 48 H940", True),
    ("M925 116 H940", True),
    ("M925 184 H940", True),
    ("M925 252 H940", True),
)


def _box(x: int, y: int, w: int, h: int, title: str, subtitle: str, tone: str) -> str:
    lines = subtitle.split("|")
    title_y = y + (h - 16 * len(lines)) / 2 + 6
    text = f'<text x="{x + w / 2}" y="{title_y}" class="arch-title">{html.escape(title)}</text>'
    text += "".join(f'<text x="{x + w / 2}" y="{title_y + 17 * (n + 1)}" class="arch-sub">{html.escape(line)}</text>' for n, line in enumerate(lines))
    return f'<g class="arch-box arch-{tone}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12"/>{text}</g>'


def _arrow(path: str, dashed: bool, head: bool = True) -> str:
    return f'<path d="{path}" class="arch-line{" arch-dashed" if dashed else ""}"{' marker-end="url(#arch-head)"' if head else ""}/>'


ARCHITECTURE_SVG = (
    '<figure class="arch"><div class="arch-scroll"><svg viewBox="0 0 1100 580" role="img" aria-labelledby="arch-title arch-desc">'
    '<title id="arch-title">RadReport architecture</title>'
    '<desc id="arch-desc">People reach the web app through a CDN. The web app reads and writes a sharded, row-level-secured Postgres, a read replica, Redis and object storage, and queues work for background workers. Workers run the 17-step report pipeline, which calls AI and speech providers. Every change writes an outbox event that a relay sends through Kafka or Postgres to consumers such as hospital export. Prometheus, OpenTelemetry, Sentry and k6 watch it all.</desc>'
    '<defs><marker id="arch-head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class="arch-head"/></marker></defs>' + "".join(_arrow(*arrow) for arrow in _ARROWS) + "".join(_box(x, y, w, h, title, subtitle, tone) for _key, x, y, w, h, title, subtitle, tone in _BOXES) + "</svg></div><figcaption>Solid arrows carry requests and work; dashed ones are monitoring. Every box marked × N runs as several copies.</figcaption></figure>"
)
