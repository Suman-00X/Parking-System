"""Large imports go out in batched statements: a 5000-report corpus loads in seconds, and re-mining reads once."""

from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import func, select

from radreport.db import instrumentation
from radreport.db.models.onboarding import CorpusReport
from radreport.db.session import tenant_session
from radreport.onboarding.corpus import CorpusRecord, load_corpus
from radreport.onboarding.lexicon import run_mining
from radreport.onboarding.roster import RosterRow, import_roster

pytestmark = pytest.mark.db

_TEXT = "FINDINGS: The liver is normal in size and echotexture. No focal lesion. LLL shows mild consolidation. IMPRESSION: Normal study {n}."


def test_a_five_thousand_report_corpus_loads_in_under_five_seconds(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    records = [CorpusRecord(external_report_id=f"R{n:05d}", report_text=_TEXT.format(n=n), is_deidentified=True) for n in range(5000)]
    with tenant_session(tenant_id, url=migrated_db) as session, instrumentation.query_scope("corpus") as stats:
        started = time.perf_counter()
        result = load_corpus(session, tenant_id=tenant_id, records=records)
        session.flush()
        elapsed = time.perf_counter() - started
        stored = session.execute(select(func.count()).select_from(CorpusReport).where(CorpusReport.tenant_id == tenant_id)).scalar_one()
    print(f"5000 reports in {elapsed:.2f}s with {stats.count} statements")
    assert result.loaded == 5000 and stored == 5000
    assert elapsed < 5.0
    assert stats.count < 30, "batches of 1000, not a statement per report"


def test_reloading_the_same_corpus_adds_nothing(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    records = [CorpusRecord(external_report_id=f"D{n}", report_text=_TEXT.format(n=n)) for n in range(20)]
    with tenant_session(tenant_id, url=migrated_db) as session:
        load_corpus(session, tenant_id=tenant_id, records=records)
        again = load_corpus(session, tenant_id=tenant_id, records=records)
    assert (again.loaded, again.duplicates) == (0, 20)


def test_term_mining_reads_the_lexicon_once(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    with tenant_session(tenant_id, url=migrated_db) as session:
        load_corpus(session, tenant_id=tenant_id, records=[CorpusRecord(external_report_id=f"M{n}", report_text=_TEXT.format(n=n)) for n in range(40)])
        first = run_mining(session, tenant_id=tenant_id, min_frequency=2)
        with instrumentation.query_scope("remine") as stats:
            second = run_mining(session, tenant_id=tenant_id, min_frequency=2)
    assert first.terms_new > 0 and second.terms_new == 0
    assert stats.count < 15, "one read of the set, not one per mined term"


def test_a_roster_import_reads_existing_staff_once(migrated_db: str, two_tenants) -> None:
    tenant_id, _ = two_tenants
    rows = [RosterRow(employee_code=f"E{n}-{uuid.uuid4().hex[:4]}", display_name=f"Dr {n}", email=None, roles=("radiologist",)) for n in range(60)]
    with tenant_session(tenant_id, url=migrated_db) as session, instrumentation.query_scope("roster") as stats:
        result = import_roster(session, tenant_id=tenant_id, rows=rows)
        session.flush()
    assert len(result.created) == 60 and len(result.profiles_created) == 60
    assert stats.count < 20
