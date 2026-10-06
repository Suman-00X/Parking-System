"""Import-batch lifecycle: every onboarding upload is a batch, tracked here from upload to applied.

Order: open a batch (open_batch) -> attach each uploaded file (register_artifact) -> move it
through the status graph (transition) -> count unresolved blocking issues
(recount_blocking_issues) -> record accepted/rejected tallies (record_counts).
An applied batch is undone with revert_batch.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radreport.core.errors import BatchBlocked, BatchStateError
from radreport.core.hashing import hash_bytes
from radreport.core.logging import get_logger
from radreport.core.types import ActorType, CollisionResolution, CollisionSeverity, ImportStatus
from radreport.db.models.onboarding import CollisionAuditFinding, ImportArtifact, ImportBatch
from radreport.db.models.orchestration import AuditLog
from radreport.db.session import ACTING_PLATFORM_USER

log = get_logger(__name__)


#: the status graph, as the set of legal moves.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {ImportStatus.UPLOADING: frozenset({ImportStatus.PARSING, ImportStatus.REJECTED}), ImportStatus.PARSING: frozenset({ImportStatus.AWAITING_REVIEW, ImportStatus.REJECTED}), ImportStatus.AWAITING_REVIEW: frozenset({ImportStatus.APPROVED, ImportStatus.REJECTED}), ImportStatus.APPROVED: frozenset({ImportStatus.APPLIED, ImportStatus.REJECTED}), ImportStatus.APPLIED: frozenset(), ImportStatus.REJECTED: frozenset()}


@dataclass(frozen=True, slots=True)
class ArtifactUpload:
    """One file offered to a batch."""

    filename: str
    data: bytes
    mime_type: str | None = None


def open_batch(session: Session, *, tenant_id: uuid.UUID, batch_type: str, stage: str, trigger: str, submitted_by: uuid.UUID | None = None) -> ImportBatch:
    """Start a batch in `uploading`, recording a product admin as its submitter when one is acting."""
    platform_user_id = session.info.get(ACTING_PLATFORM_USER) if submitted_by is None else None
    batch = ImportBatch(tenant_id=tenant_id, batch_type=batch_type, trigger=trigger, stage=stage, status=ImportStatus.UPLOADING, submitted_by=submitted_by, submitted_by_platform_user_id=platform_user_id)
    session.add(batch)
    session.flush()
    log.info("import_batch_opened", tenant_id=str(tenant_id), batch_id=str(batch.id), stage=stage, batch_type=batch_type, trigger=trigger)
    return batch


def register_artifact(session: Session, batch: ImportBatch, upload: ArtifactUpload, *, object_key: str | None = None) -> tuple[ImportArtifact, bool]:
    """Attach a file to a batch, idempotently. Returns `(artifact, created)`."""
    content_hash = hash_bytes(upload.data)
    existing = session.execute(select(ImportArtifact).where(ImportArtifact.tenant_id == batch.tenant_id, ImportArtifact.import_batch_id == batch.id, ImportArtifact.content_hash == content_hash)).scalar_one_or_none()
    if existing is not None:
        log.info("import_artifact_duplicate", batch_id=str(batch.id), content_hash=content_hash, artifact_id=str(existing.id))
        return existing, False

    artifact = ImportArtifact(tenant_id=batch.tenant_id, import_batch_id=batch.id, original_filename=upload.filename, content_hash=content_hash, mime_type=upload.mime_type, object_key=object_key)
    session.add(artifact)
    batch.item_count += 1
    session.flush()
    return artifact, True


def transition(session: Session, batch: ImportBatch, target: str, *, actor_id: uuid.UUID | None = None) -> ImportBatch:
    """Move a batch through the status graph, enforcing the gates."""
    current = batch.status
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise BatchStateError(str(batch.id), current, target)

    if target == ImportStatus.APPLIED:
        outstanding = recount_blocking_issues(session, batch)
        if outstanding:
            raise BatchBlocked(str(batch.id), outstanding)
        batch.applied_at = dt.datetime.now(dt.UTC)

    if target == ImportStatus.APPROVED:
        # Approval is a clinical act — record who, not merely that.
        batch.approved_by = actor_id

    batch.status = target
    session.add(AuditLog(tenant_id=batch.tenant_id, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action="import_batch_status_changed", entity_type="import_batch", entity_id=batch.id, before={"status": current}, after={"status": target, "stage": batch.stage}))
    session.flush()
    log.info("import_batch_status_changed", batch_id=str(batch.id), stage=batch.stage, previous=current, target=target)
    return batch


def recount_blocking_issues(session: Session, batch: ImportBatch) -> int:
    """Re-derive `blocking_issue_count` from `collision_audit_finding`."""
    outstanding = session.execute(select(func.count()).select_from(CollisionAuditFinding).where(CollisionAuditFinding.tenant_id == batch.tenant_id, CollisionAuditFinding.import_batch_id == batch.id, CollisionAuditFinding.severity == CollisionSeverity.BLOCK, CollisionAuditFinding.resolution == CollisionResolution.PENDING)).scalar_one()
    batch.blocking_issue_count = outstanding
    session.flush()
    return outstanding


def revert_batch(session: Session, batch: ImportBatch, *, actor_id: uuid.UUID | None = None) -> ImportBatch:
    """The onboarding rollback: stamp `reverted_at`."""
    if batch.status != ImportStatus.APPLIED:
        raise BatchStateError(str(batch.id), batch.status, "reverted")

    batch.reverted_at = dt.datetime.now(dt.UTC)
    session.add(AuditLog(tenant_id=batch.tenant_id, actor_id=actor_id, actor_type=ActorType.USER if actor_id else ActorType.SYSTEM, action="import_batch_reverted", entity_type="import_batch", entity_id=batch.id, before={"applied_at": str(batch.applied_at)}, after={"reverted_at": str(batch.reverted_at), "stage": batch.stage}))
    session.flush()
    log.warning("import_batch_reverted", batch_id=str(batch.id), stage=batch.stage)
    return batch


def record_counts(session: Session, batch: ImportBatch, *, accepted: int = 0, rejected: int = 0) -> None:
    """Accumulate the per-batch tallies the admin panel reports."""
    batch.accepted_count += accepted
    batch.rejected_count += rejected
    session.flush()
