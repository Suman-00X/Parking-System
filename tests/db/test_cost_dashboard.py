"""The cost dashboard against real runs: totals that add up, stage breakdown, spikes, the pages and the anomaly events."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from radreport.core.tenancy import CROSS_TENANT_FUNCTIONS
from radreport.db.session import system_session, tenant_session
from radreport.devtools.cost_history import write_history
from radreport.monitoring import costs
from tests.db.helpers import make_platform_user, signed_in

pytestmark = pytest.mark.db

#: A Wednesday, so the planted spike two days earlier falls on a busy weekday.
TODAY = dt.date(2026, 7, 15)


@pytest.fixture
def history(migrated_db: str, two_tenants):
    lab, quiet = two_tenants
    write_history(lab, days=40, spike_days_ago=2, url=migrated_db, today=TODAY)
    return lab, quiet


def test_a_labs_totals_add_up_and_its_spike_is_found(migrated_db: str, history) -> None:
    lab, _quiet = history
    with system_session(migrated_db) as session:
        summary = costs.lab_summary(session, lab, days=30, today=TODAY)
    with tenant_session(lab, url=migrated_db) as session:
        stored = float(session.execute(text("SELECT sum(total_cost_usd) FROM pipeline_run WHERE created_at >= :a AND created_at < :b"), {"a": TODAY - dt.timedelta(days=29), "b": TODAY + dt.timedelta(days=1)}).scalar_one())
    assert summary["total_cost_usd"] == pytest.approx(stored, abs=0.01)
    assert summary["runs"] > 0 and summary["avg_cost_per_report_usd"] > 0
    assert [s.day for s in summary["spikes"]] == [TODAY - dt.timedelta(days=2)]
    extract = next(st for st in summary["stages"] if st["stage_name"] == "extract")
    assert extract["cost_usd"] > 0 and 0 < extract["cache_hit_ratio"] < 1


def test_the_platform_view_ranks_labs_and_compares_periods(migrated_db: str, history) -> None:
    lab, quiet = history
    with system_session(migrated_db) as session:
        summary = costs.platform_summary(session, days=30, today=TODAY)
    ours = next(entry for entry in summary["labs"] if entry.tenant_id == lab)
    assert ours.cost_usd > 0 and ours.previous_cost_usd > 0 and ours.change is not None
    assert all(entry.tenant_id != quiet for entry in summary["labs"]), "a lab with no runs is not listed"
    assert len(summary["series"]) == 30


def test_the_pages_and_the_api(migrated_db: str, history) -> None:
    lab, _quiet = history
    client = signed_in(make_platform_user(migrated_db))
    page = client.get("/admin/costs", params={"days": 90})
    assert page.status_code == 200 and "Platform spend" in page.text and "Cost &amp; usage" in page.text
    lab_page = client.get("/admin/costs", params={"lab": str(lab), "days": 90})
    assert lab_page.status_code == 200 and "Where the money goes" in lab_page.text
    api = client.get("/admin/api/costs", params={"tenant_id": str(lab), "days": 90}).json()
    assert api["tenant_id"] == str(lab) and api["series"] and api["stages"]
    assert client.get("/admin/api/costs", params={"days": 12}).status_code == 400


def test_new_spikes_are_announced_once(migrated_db: str, history) -> None:
    lab, _quiet = history
    with system_session(migrated_db) as session:
        first = costs.scan_for_anomalies(session, today=TODAY, url=migrated_db)
    with system_session(migrated_db) as session:
        second = costs.scan_for_anomalies(session, today=TODAY, url=migrated_db)
    assert [a["tenant_id"] for a in first] == [str(lab)] and second == []
    with tenant_session(lab, url=migrated_db) as session:
        assert session.execute(text("SELECT count(*) FROM outbox_event WHERE topic = 'cost.anomaly' AND tenant_id = :t"), {"t": lab}).scalar_one() == 1


def test_every_cross_lab_function_is_enumerated(migrated_db: str) -> None:
    """A function that can read every lab's rows exists only if it is named in core/tenancy.py."""
    with system_session(migrated_db) as session:
        owned = set(session.execute(text("SELECT p.proname FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner WHERE r.rolname = 'radreport_views' AND p.prosecdef")).scalars())
    assert owned == set(CROSS_TENANT_FUNCTIONS)


def test_the_materialized_eval_set_refreshes(migrated_db: str) -> None:
    with system_session(migrated_db) as session:
        assert session.execute(text("SELECT refresh_canonical_eval_set()")).scalar_one() >= 0


def test_the_admin_navigation_reaches_every_operations_page(migrated_db: str) -> None:
    page = signed_in(make_platform_user(migrated_db)).get("/admin/labs").text
    for href in ('href="/admin/costs"', 'href="/admin/config"', 'href="/admin/providers"', 'href="/admin/users"'):
        assert href in page
