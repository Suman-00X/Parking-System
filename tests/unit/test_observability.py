"""Metrics, the scrape endpoint, the error-report scrubber and the span helper, without a database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from radreport.api.app import create_app
from radreport.cache.request import HitStats
from radreport.core.config import get_settings
from radreport.observability import metrics as prom
from radreport.observability.errors import _scrub
from radreport.observability.tracing import span


def _value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()
    monkeypatch.setattr(prom, "_count_backlog", lambda: [])
    yield monkeypatch
    get_settings.cache_clear()


# ====================================================== the endpoint ===
def test_metrics_answer_on_a_development_deployment(env: pytest.MonkeyPatch) -> None:
    env.setenv("RADREPORT_ENVIRONMENT", "local")
    env.delenv("RADREPORT_OBSERVABILITY__METRICS_TOKEN", raising=False)
    page = TestClient(create_app()).get("/metrics")
    assert page.status_code == 200
    assert "radreport_http_requests_total" in page.text


def test_metrics_are_hidden_in_production_without_a_token(env: pytest.MonkeyPatch) -> None:
    env.setenv("RADREPORT_ENVIRONMENT", "local")
    client = TestClient(create_app())
    env.setenv("RADREPORT_ENVIRONMENT", "production")
    get_settings.cache_clear()
    assert client.get("/metrics").status_code == 404


def test_a_configured_token_is_required(env: pytest.MonkeyPatch) -> None:
    env.setenv("RADREPORT_ENVIRONMENT", "local")
    env.setenv("RADREPORT_OBSERVABILITY__METRICS_TOKEN", "scrape-me-please")
    client = TestClient(create_app())
    assert client.get("/metrics").status_code == 404
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 404
    assert client.get("/metrics", headers={"Authorization": "Bearer scrape-me-please"}).status_code == 200


def test_requests_are_labelled_by_policy_route_not_by_path(env: pytest.MonkeyPatch) -> None:
    env.setenv("RADREPORT_ENVIRONMENT", "local")
    client = TestClient(create_app())
    before = _value("radreport_http_requests_total", route="public.features", method="GET", status="2xx")
    unmatched = _value("radreport_http_requests_total", route="unmatched", method="GET", status="4xx")
    client.get("/features")
    client.get("/no/such/page/123")
    assert _value("radreport_http_requests_total", route="public.features", method="GET", status="2xx") == before + 1
    assert _value("radreport_http_requests_total", route="unmatched", method="GET", status="4xx") == unmatched + 1


# ===================================================== the recorders ===
def test_a_stage_records_its_time_tokens_and_spend() -> None:
    before = _value("radreport_llm_tokens_total", stage="unit-extract", kind="input")
    cost = _value("radreport_llm_cost_usd_total", stage="unit-extract")
    prom.observe_stage("unit-extract", "succeeded", 120, tokens_in=500, tokens_out=40, cost_usd=0.002)
    assert _value("radreport_llm_tokens_total", stage="unit-extract", kind="input") == before + 500
    assert _value("radreport_llm_cost_usd_total", stage="unit-extract") == pytest.approx(cost + 0.002)
    assert _value("radreport_pipeline_stage_duration_seconds_count", stage="unit-extract", status="succeeded") >= 1


def test_a_named_cache_exports_its_hits_and_misses() -> None:
    stats = HitStats(name="unit-cache")
    stats.hit()
    stats.miss()
    stats.miss()
    assert _value("radreport_cache_requests_total", cache="unit-cache", result="hit") == 1
    assert _value("radreport_cache_requests_total", cache="unit-cache", result="miss") == 2


def test_backlog_is_reported_per_kind_and_state() -> None:
    rows = [("job", "run_pipeline", "queued", 7, 42.0), ("job", "run_pipeline", "dead", 1, 900.0), ("outbox", "report.signed", "unsent", 3, 5.5)]
    body, _ = prom.render(backlog=prom.BacklogCollector(lambda: rows))
    text = body.decode()
    assert 'radreport_jobs_backlog{kind="run_pipeline",state="queued"} 7.0' in text
    assert 'radreport_jobs_backlog{kind="run_pipeline",state="dead"} 1.0' in text
    assert 'radreport_outbox_unsent{topic="report.signed"} 3.0' in text


def test_a_failed_backlog_count_is_left_out_not_reported_as_zero() -> None:
    def broken() -> list:
        raise ConnectionError("database unreachable")

    body, _ = prom.render(backlog=prom.BacklogCollector(broken))
    assert "radreport_jobs_backlog" not in body.decode()
    assert "radreport_http_requests_total" in body.decode()


# ======================================================= the scrubber ===
def test_error_reports_carry_no_body_credentials_or_message() -> None:
    event = {"request": {"url": "https://x/review/drafts/1", "data": "large pneumothorax", "cookies": {"radreport_admin": "t"}, "query_string": "mrn=1", "headers": {"Authorization": "Bearer t", "Cookie": "a=b", "User-Agent": "k6"}}, "user": {"email": "a@b.c"}, "exception": {"values": [{"type": "StageFailed", "value": "extract: no 3.2 cm nodule", "stacktrace": {"frames": []}}]}, "breadcrumbs": {"values": [{"message": "transcript ..."}]}}
    scrubbed = _scrub(event, {})
    assert set(scrubbed["request"]) == {"url", "headers"}
    assert scrubbed["request"]["headers"] == {"User-Agent": "k6"}
    assert "user" not in scrubbed and "breadcrumbs" not in scrubbed
    assert scrubbed["exception"]["values"][0] == {"type": "StageFailed", "value": "<redacted>", "stacktrace": {"frames": []}}


def test_a_span_is_a_no_op_when_tracing_is_off() -> None:
    with span("stage unit", stage="unit"):
        ran = True
    assert ran
