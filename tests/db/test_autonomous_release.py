"""Releasing a report with no reviewer, against a real database.

The unit tests cover the decision logic; these cover what only a database can refuse.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from radreport.autonomy import release
from radreport.core.types import AutonomyStatus, ExportStatus, PathType, SeverityGrade, UserRole
from radreport.db.models.identity import AppUser
from radreport.db.models.knowledge import AutonomyClass
from radreport.db.models.review import FinalReport
from radreport.db.session import tenant_session
from radreport.review import grading, signing
from radreport.review.rbac import Reviewer
from tests.db.review_factory import build_signed_report

pytestmark = pytest.mark.db


@pytest.fixture
def autonomy_fixture(migrated_db: str, two_tenants) -> tuple[str, uuid.UUID]:
    """One tenant on a migrated database."""
    tenant_id, _other = two_tenants
    return migrated_db, tenant_id


def _released_report(session, tenant_id: uuid.UUID, *, klass: AutonomyClass) -> FinalReport:
    """A report filed on the autonomous path, as stage 17 would file it."""
    built = build_signed_report(session, tenant_id)
    # Reuse the factory's study/draft, then file the autonomous row beside it.
    original = session.get(FinalReport, built["final_report_id"])
    assert original is not None

    report = FinalReport(id=uuid.uuid4(), tenant_id=tenant_id, study_id=original.study_id, report_draft_id=original.report_draft_id, final_revision_id=None, signed_by=original.signed_by, signed_at=dt.datetime.now(dt.UTC), rendered_text="FINDINGS\nLiver: normal", structured_payload={}, content_hash=uuid.uuid4().hex, path_type=PathType.AUTONOMOUS, autonomy_class_id=klass.id, export_status=ExportStatus.PENDING)
    session.add(report)
    session.flush()
    return report


def _granted_class(session, tenant_id: uuid.UUID) -> AutonomyClass:
    klass = AutonomyClass(tenant_id=tenant_id, code=f"CLASS-{uuid.uuid4().hex[:6]}", display_name="Abdominal ultrasound", status=AutonomyStatus.GRANTED, baseline_cse_rate=0.025, ni_margin_pp=1.0, required_n=100, cusum_threshold=3.0)
    session.add(klass)
    session.flush()
    return klass


def test_an_autonomous_report_may_have_no_revision(autonomy_fixture) -> None:
    """The null migration 0006 exists for."""
    migrated_db, one_tenant = autonomy_fixture
    with tenant_session(one_tenant, url=migrated_db) as session:
        klass = _granted_class(session, one_tenant)
        report = _released_report(session, one_tenant, klass=klass)

        assert report.final_revision_id is None
        assert report.path_type == PathType.AUTONOMOUS
        assert report.autonomy_class_id == klass.id


def test_a_reviewed_report_may_not_lose_its_revision(autonomy_fixture) -> None:
    """The other direction of the CHECK. A null revision on a reviewed report would be a lost audit trail, not a release."""
    migrated_db, one_tenant = autonomy_fixture
    with pytest.raises(IntegrityError):  # noqa: PT012 - the flush is the assertion
        with tenant_session(one_tenant, url=migrated_db) as session:
            built = build_signed_report(session, one_tenant)
            report = session.get(FinalReport, built["final_report_id"])
            assert report is not None
            report.final_revision_id = None
            session.flush()


def test_an_autonomous_report_may_not_claim_a_review(autonomy_fixture) -> None:
    """An autonomous report that names a revision is claiming a review that did not happen, which is the direction a reader would never think to check."""
    migrated_db, one_tenant = autonomy_fixture
    with pytest.raises(IntegrityError):  # noqa: PT012 - the flush is the assertion
        with tenant_session(one_tenant, url=migrated_db) as session:
            klass = _granted_class(session, one_tenant)
            report = _released_report(session, one_tenant, klass=klass)
            report.final_revision_id = uuid.uuid4()
            session.flush()


def test_grading_a_released_report_still_feeds_the_monitor(autonomy_fixture) -> None:
    """The released volume's only error signal."""
    migrated_db, one_tenant = autonomy_fixture
    with tenant_session(one_tenant, url=migrated_db) as session:
        klass = _granted_class(session, one_tenant)
        report = _released_report(session, one_tenant, klass=klass)

        radiologist = AppUser(tenant_id=one_tenant, employee_code=f"R-{uuid.uuid4().hex[:6]}", display_name="Dr Grader", roles=[UserRole.RADIOLOGIST])
        session.add(radiologist)
        session.flush()
        reviewer = Reviewer(user_id=radiologist.id, roles=(UserRole.RADIOLOGIST,))

        result = grading.grade_report(session, tenant_id=one_tenant, final_report_id=report.id, grade=SeverityGrade.G4, reviewer=reviewer)

        assert result.is_cse is True
        assert result.edit_events_graded == 0, "a released report has no edit events"
        # The observation is what the CUSUM reads, and it exists.
        session.refresh(klass)
        assert float(klass.cusum_statistic) > 0


def test_a_grade_feeds_the_class_that_released_the_report(autonomy_fixture) -> None:
    """If the template has since moved to another class, the evidence still belongs to the releasing one."""
    from radreport.db.models.knowledge import Template, TemplateVersion
    from radreport.db.models.reporting import ReportDraft

    migrated_db, one_tenant = autonomy_fixture
    with tenant_session(one_tenant, url=migrated_db) as session:
        releasing, later = _granted_class(session, one_tenant), _granted_class(session, one_tenant)
        report = _released_report(session, one_tenant, klass=releasing)
        draft = session.get(ReportDraft, report.report_draft_id)
        template = session.get(Template, session.get(TemplateVersion, draft.template_version_id).template_id)
        template.autonomy_class_id = later.id
        session.flush()

        radiologist = AppUser(tenant_id=one_tenant, employee_code=f"R-{uuid.uuid4().hex[:6]}", display_name="Dr Grader", roles=[UserRole.RADIOLOGIST])
        session.add(radiologist)
        session.flush()
        grading.grade_report(session, tenant_id=one_tenant, final_report_id=report.id, grade=SeverityGrade.G4, reviewer=Reviewer(user_id=radiologist.id, roles=(UserRole.RADIOLOGIST,)))

        session.refresh(releasing)
        session.refresh(later)
        assert float(releasing.cusum_statistic) > 0
        assert float(later.cusum_statistic or 0) == 0


def test_an_addendum_to_a_released_report_is_not_autonomous(autonomy_fixture) -> None:
    """A radiologist wrote and signed it, so claiming it went out unreviewed would be the one case where the audit gets a wrong answer."""
    migrated_db, one_tenant = autonomy_fixture
    with tenant_session(one_tenant, url=migrated_db) as session:
        klass = _granted_class(session, one_tenant)
        report = _released_report(session, one_tenant, klass=klass)

        radiologist = session.get(AppUser, report.signed_by)
        assert radiologist is not None
        reviewer = Reviewer(user_id=radiologist.id, roles=(UserRole.RADIOLOGIST,))

        addendum = signing.amend_report(session, tenant_id=one_tenant, original_report_id=report.id, reviewer=reviewer, rendered_text="FINDINGS\nLiver: 2 cm cyst, previously missed.", reason="missed finding")

        assert addendum.path_type == PathType.RADIOLOGIST_ONLY
        assert addendum.amends_report_id == report.id
        # Permitted by the addendum exemption in the CHECK: an addendum's text
        # comes from the argument, not from a revision.
        assert addendum.final_revision_id is None


def test_coverage_counts_released_share_of_signed_volume(autonomy_fixture) -> None:
    """The metric, over the denominator actually claims."""
    migrated_db, one_tenant = autonomy_fixture
    with tenant_session(one_tenant, url=migrated_db) as session:
        klass = _granted_class(session, one_tenant)
        # One reviewed report from the factory, one released beside it.
        _released_report(session, one_tenant, klass=klass)

        measured = release.coverage(session, tenant_id=one_tenant)
        assert measured.signed == 2
        assert measured.released == 1
        assert measured.reviewed == 1
        assert measured.share == 0.5
        assert measured.meets_target is True


def test_coverage_excludes_addenda_from_the_denominator(autonomy_fixture) -> None:
    """An addendum corrects a report already counted."""
    migrated_db, one_tenant = autonomy_fixture
    with tenant_session(one_tenant, url=migrated_db) as session:
        klass = _granted_class(session, one_tenant)
        report = _released_report(session, one_tenant, klass=klass)
        before = release.coverage(session, tenant_id=one_tenant)

        radiologist = session.get(AppUser, report.signed_by)
        assert radiologist is not None
        signing.amend_report(session, tenant_id=one_tenant, original_report_id=report.id, reviewer=Reviewer(user_id=radiologist.id, roles=(UserRole.RADIOLOGIST,)), rendered_text="addendum", reason="correction")

        after = release.coverage(session, tenant_id=one_tenant)
        assert after.signed == before.signed
        assert after.share == before.share
