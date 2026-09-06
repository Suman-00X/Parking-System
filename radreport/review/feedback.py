"""The one-click "this draft was not useful" signal, and what it adds up to.

Order: record one reviewer's verdict (report_usefulness) -> summarise them (usefulness_stats).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radreport.core.logging import get_logger
from radreport.core.types import ActorType
from radreport.db.models.orchestration import AuditLog
from radreport.db.models.reporting import ReportDraft
from radreport.db.models.review import DraftUsefulnessReport
from radreport.review.rbac import Permission, Reviewer, require

log = get_logger(__name__)

#: Above this share of drafts reported useless, the flywheel is stalling and
#: somebody should look at *why* rather than at the aggregate.
DISENGAGEMENT_ALARM_RATE = 0.20


@dataclass(frozen=True, slots=True)
class UsefulnessStats:
    reported: int
    useless: int
    reasons: tuple[str, ...]

    @property
    def rate(self) -> float:
        return round(self.useless / self.reported, 4) if self.reported else 0.0

    @property
    def is_alarming(self) -> bool:
        return self.reported >= 20 and self.rate >= DISENGAGEMENT_ALARM_RATE


def report_usefulness(session: Session, *, tenant_id: uuid.UUID, draft_id: uuid.UUID, reviewer: Reviewer, was_useless: bool, reason: str | None = None) -> DraftUsefulnessReport:
    """Record a reviewer's verdict on whether the draft helped."""
    require(reviewer, Permission.REPORT_USELESS)

    draft = session.get(ReportDraft, draft_id)
    if draft is None or draft.tenant_id != tenant_id:
        raise ValueError(f"no report_draft {draft_id} in this tenant")

    existing = session.execute(select(DraftUsefulnessReport).where(DraftUsefulnessReport.tenant_id == tenant_id, DraftUsefulnessReport.report_draft_id == draft_id, DraftUsefulnessReport.reported_by == reviewer.user_id)).scalar_one_or_none()

    if existing is not None:
        existing.was_useless = was_useless
        existing.reason = reason
        record = existing
    else:
        record = DraftUsefulnessReport(tenant_id=tenant_id, report_draft_id=draft_id, reported_by=reviewer.user_id, was_useless=was_useless, reason=reason)
        session.add(record)

    session.add(AuditLog(tenant_id=tenant_id, actor_id=reviewer.user_id, actor_type=ActorType.USER, action="draft_usefulness_reported", entity_type="report_draft", entity_id=draft_id, after={"was_useless": was_useless, "reason": reason}))
    session.flush()

    if was_useless:
        # Logged at warning level on purpose: this is the signal is about,
        # and it should be visible without anyone running a query.
        log.warning("draft_reported_useless", draft_id=str(draft_id), reported_by=str(reviewer.user_id), reason=reason)
    return record


def usefulness_stats(session: Session, *, tenant_id: uuid.UUID) -> UsefulnessStats:
    """The disengagement early warning."""
    reported = session.execute(select(func.count()).select_from(DraftUsefulnessReport).where(DraftUsefulnessReport.tenant_id == tenant_id)).scalar_one()

    useless_rows = list(session.execute(select(DraftUsefulnessReport).where(DraftUsefulnessReport.tenant_id == tenant_id, DraftUsefulnessReport.was_useless.is_(True))).scalars().all())

    stats = UsefulnessStats(reported=reported, useless=len(useless_rows), reasons=tuple(r.reason for r in useless_rows if r.reason)[:20])
    if stats.is_alarming:
        log.warning("reviewer_disengagement_signal", tenant_id=str(tenant_id), rate=stats.rate, reported=stats.reported, detail=": the edit corpus may be describing disengagement, not model error")
    return stats
