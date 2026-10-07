"""The list of drafts waiting for review, ordered so the ones needing attention come first.

Order: score each draft for position (sort_key) -> build the ordered list (build_queue) ->
summarise it for the screen header (queue_stats, count_by_status).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radreport.core.types import AlertSeverity, DraftStatus, PathType, StudyPriority
from radreport.db.models.identity import Study
from radreport.db.models.ingestion import Recording
from radreport.db.models.knowledge import Template, TemplateVersion
from radreport.db.models.reporting import CriticalFindingAlert, CriticalFindingRule, ReportDraft
from radreport.db.models.review import FinalReport
from radreport.review.rbac import Permission, Reviewer, require

#: the threshold, repeated here because the queue shows the consequence.
RADIOLOGIST_ONLY_BELOW = 0.70

_PRIORITY_RANK: dict[str, int] = {StudyPriority.STAT: 0, StudyPriority.URGENT: 1, StudyPriority.ROUTINE: 2}


@dataclass(frozen=True, slots=True)
class QueueItem:
    draft_id: uuid.UUID
    recording_id: uuid.UUID
    study_id: uuid.UUID
    template_code: str
    template_display_name: str
    priority: str
    confidence: float
    flagged_field_count: int
    status: str
    created_at: dt.datetime
    has_critical_alert: bool
    critical_alert_severity: str | None
    path_type: str
    """Which review path assigned. Shown so an assistant can see at a glance which drafts are not theirs to take."""

    waiting_minutes: int

    @property
    def requires_radiologist(self) -> bool:
        return self.path_type == PathType.RADIOLOGIST_ONLY

    @property
    def released_without_review(self) -> bool:
        """Filed under a granted autonomy class, with nobody assigned."""
        return self.path_type == PathType.AUTONOMOUS


def sort_key(item: QueueItem) -> tuple[int, int, float, dt.datetime]:
    """Priority → critical alert → flagged count → oldest first."""
    return (_PRIORITY_RANK.get(item.priority, 3), 0 if item.has_critical_alert else 1, -item.flagged_field_count, item.created_at)


def build_queue(session: Session, *, tenant_id: uuid.UUID, reviewer: Reviewer, limit: int = 50, include_signed: bool = False) -> list[QueueItem]:
    """The reviewer's work list, ordered and filtered by role."""
    require(reviewer, Permission.VIEW_QUEUE)

    statuses = [DraftStatus.GENERATED, DraftStatus.IN_REVIEW, DraftStatus.REVISED]
    if include_signed:
        statuses.append(DraftStatus.SIGNED)

    rows = session.execute(select(ReportDraft, Recording, Study, Template).join(Recording, Recording.id == ReportDraft.recording_id).join(Study, Study.id == Recording.study_id).join(TemplateVersion, TemplateVersion.id == ReportDraft.template_version_id).join(Template, Template.id == TemplateVersion.template_id).where(ReportDraft.tenant_id == tenant_id, ReportDraft.status.in_(statuses))).all()

    # An autonomously released draft is written `signed` by stage 16, so it is already excluded by the status filter above unless the caller asked for signed drafts.
    autonomous_draft_ids: frozenset[uuid.UUID] = frozenset()
    if include_signed:
        autonomous_draft_ids = frozenset(session.execute(select(FinalReport.report_draft_id).where(FinalReport.tenant_id == tenant_id, FinalReport.path_type == PathType.AUTONOMOUS)).scalars().all())

    alerts = _alerts_by_recording(session, tenant_id, [recording.id for _, recording, _, _ in rows])
    now = dt.datetime.now(dt.UTC)
    items: list[QueueItem] = []

    for draft, recording, study, template in rows:
        alert_severity = alerts.get(recording.id)
        confidence = float(draft.overall_confidence)
        if draft.id in autonomous_draft_ids:
            path_type = PathType.AUTONOMOUS
        else:
            path_type = PathType.RADIOLOGIST_ONLY if alert_severity is not None or confidence < RADIOLOGIST_ONLY_BELOW else PathType.TRANSCRIPTIONIST_REVIEWED
        created = draft.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.UTC)

        items.append(QueueItem(draft_id=draft.id, recording_id=recording.id, study_id=study.id, template_code=template.code, template_display_name=template.display_name, priority=study.priority, confidence=confidence, flagged_field_count=draft.flagged_field_count, status=draft.status, created_at=created, has_critical_alert=alert_severity is not None, critical_alert_severity=alert_severity, path_type=path_type, waiting_minutes=int((now - created).total_seconds() // 60)))

    if not reviewer.is_radiologist:
        items = [i for i in items if not i.requires_radiologist]

    return sorted(items, key=sort_key)[:limit]


def _alerts_by_recording(session: Session, tenant_id: uuid.UUID, recording_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """`recording_id -> worst severity` for the queued recordings only. Red outranks orange."""
    if not recording_ids:
        return {}
    rows = session.execute(select(CriticalFindingAlert.recording_id, CriticalFindingRule.severity).join(CriticalFindingRule, CriticalFindingRule.id == CriticalFindingAlert.rule_id).where(CriticalFindingAlert.tenant_id == tenant_id, CriticalFindingAlert.recording_id.in_(recording_ids))).all()
    worst: dict[uuid.UUID, str] = {}
    for recording_id, severity in rows:
        if worst.get(recording_id) != AlertSeverity.RED:
            worst[recording_id] = severity
    return worst


@dataclass(frozen=True, slots=True)
class QueueStats:
    """What a lab admin sees: throughput, not clinical content."""

    total: int
    awaiting_radiologist: int
    awaiting_assistant: int
    with_critical_alert: int
    oldest_waiting_minutes: int
    median_flagged_fields: float


def queue_stats(session: Session, *, tenant_id: uuid.UUID, reviewer: Reviewer) -> QueueStats:
    """Operational oversight without clinical access."""
    require(reviewer, Permission.VIEW_QUEUE)
    items = build_queue(session, tenant_id=tenant_id, reviewer=reviewer, limit=10_000)
    if not items:
        return QueueStats(0, 0, 0, 0, 0, 0.0)

    flagged = sorted(i.flagged_field_count for i in items)
    middle = len(flagged) // 2
    median = float(flagged[middle]) if len(flagged) % 2 else (flagged[middle - 1] + flagged[middle]) / 2
    return QueueStats(total=len(items), awaiting_radiologist=sum(1 for i in items if i.requires_radiologist), awaiting_assistant=sum(1 for i in items if not i.requires_radiologist), with_critical_alert=sum(1 for i in items if i.has_critical_alert), oldest_waiting_minutes=max(i.waiting_minutes for i in items), median_flagged_fields=median)


def count_by_status(session: Session, *, tenant_id: uuid.UUID) -> dict[str, int]:
    rows = session.execute(select(ReportDraft.status, func.count()).where(ReportDraft.tenant_id == tenant_id).group_by(ReportDraft.status)).all()
    return {status: int(n) for status, n in rows}
