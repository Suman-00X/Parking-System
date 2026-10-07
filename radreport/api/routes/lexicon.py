"""The lab side of lexicon growth: the terms radiologists keep using that the lexicon lacks, and their approval.

Order: list what is waiting (list_candidates) -> look again now rather than at tonight's scan
(scan_now) -> approve terms into the next lexicon version, or reject them (approve_candidates,
reject_candidates). Approval is a radiologist's call; lab admins can see the queue.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from radreport.api.deps import CurrentPrincipal, DbSession
from radreport.api.pagination import Page, paginate, set_page_headers
from radreport.api.routes.ga import _require_lab_role
from radreport.core.types import UserRole
from radreport.onboarding import term_watch

router = APIRouter(prefix="/lexicon", tags=["lexicon"])


def _out(row: Any) -> dict[str, Any]:
    return {"id": str(row.id), "term": row.surface_text, "term_type": row.term_type, "frequency": row.frequency, "contexts": list(row.contexts or []), "first_seen_at": row.first_seen_at.isoformat(), "last_seen_at": row.last_seen_at.isoformat()}


@router.get("/candidates")
def list_candidates(session: DbSession, principal: CurrentPrincipal, response: Response, min_frequency: int | None = None, page: int | None = None, page_size: int | None = None) -> list[dict[str, Any]]:
    """Terms waiting for a radiologist, most used first."""
    _require_lab_role(session, principal, UserRole.RADIOLOGIST, UserRole.LAB_ADMIN)
    assert principal.tenant_id is not None
    paged = paginate(session, term_watch.pending(session, principal.tenant_id, min_frequency=min_frequency or 1), Page.of(page, page_size))
    set_page_headers(response, paged, "/lexicon/candidates")
    return [_out(r) for r in paged.rows]


@router.post("/scan")
def scan_now(session: DbSession, principal: CurrentPrincipal) -> dict[str, int]:
    """Read the edits made since the last scan now."""
    _require_lab_role(session, principal, UserRole.RADIOLOGIST, UserRole.LAB_ADMIN)
    assert principal.tenant_id is not None
    return term_watch.scan_edits(session, principal.tenant_id)


class Decision(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


@router.post("/candidates/approve")
def approve_candidates(body: Decision, session: DbSession, principal: CurrentPrincipal) -> dict[str, Any]:
    """Add the terms to the lexicon; the lab gets a new lexicon version that includes them."""
    approver = _require_lab_role(session, principal, UserRole.RADIOLOGIST)
    assert principal.tenant_id is not None
    try:
        successor = term_watch.approve(session, principal.tenant_id, body.ids, approver_id=approver)
    except term_watch.DecisionRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"lexicon_set_id": str(successor.id), "version": successor.version, "approved": len(body.ids)}


@router.post("/candidates/reject")
def reject_candidates(body: Decision, session: DbSession, principal: CurrentPrincipal) -> dict[str, int]:
    """Set terms aside: a typo, a one-off, or not vocabulary."""
    reviewer = _require_lab_role(session, principal, UserRole.RADIOLOGIST)
    assert principal.tenant_id is not None
    try:
        return {"rejected": term_watch.reject(session, principal.tenant_id, body.ids, reviewer_id=reviewer)}
    except term_watch.DecisionRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
