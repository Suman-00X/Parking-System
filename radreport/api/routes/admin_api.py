"""The admin panel's JSON API: the same operations as its pages, for scripts and tests.

Order: labs (list_labs, register_lab_json, readiness, change_status) -> models per step
(steps, propose, activate) -> onboarding (onboarding_status, upload_roster, upload_templates,
upload_corpus, merge_proposals, run_onboarding_step) -> lab users' sign-in (list_lab_users,
set_lab_user_password) -> autonomy and adaptation (get_accrual,
open_accrual, grant_autonomy, revoke_autonomy, adaptation_gates, require_adaptation_gates) ->
platform users (list_users, create_user, deactivate_user, reactivate_user, reset_user_password,
change_own_password).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from radreport.adaptation.gates import AdaptationBlocked, evaluate_gates, require_gates
from radreport.adapters.llm.registry import activate_assignment
from radreport.admin import onboarding_steps, users
from radreport.admin.auth import MIN_PASSWORD_LENGTH
from radreport.admin.modelconfig import ConfigRefused, propose_assignment, step_configuration
from radreport.admin.onboarding_steps import StepRefused
from radreport.api.deps import AdminLabDb, CurrentAdmin
from radreport.api.pagination import Page, paginate, set_page_headers
from radreport.auth import lab as lab_auth
from radreport.autonomy import accrual, grant
from radreport.autonomy.grant import GrantRefused
from radreport.core.errors import ModelResolutionError, UngatedActivation
from radreport.core.tenancy import TenantTransitionError
from radreport.core.types import AdaptationTarget, ImportTrigger
from radreport.db.models.identity import AppUser
from radreport.db.models.tenancy import PlatformUser, Tenant
from radreport.db.session import read_session, system_session
from radreport.onboarding import corpus
from radreport.onboarding.batches import ArtifactUpload
from radreport.onboarding.readiness import evaluate_readiness
from radreport.onboarding.registration import LabRegistration, register_lab, transition_status

router = APIRouter(prefix="/admin/api", tags=["admin-api"])

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"


def _refused(exc: StepRefused | users.UserChangeRefused | ConfigRefused) -> HTTPException:
    if isinstance(exc, StepRefused):
        return HTTPException(exc.status_code, exc.reason)
    code = status.HTTP_404_NOT_FOUND if getattr(exc, "code", "") == "not_found" else status.HTTP_409_CONFLICT if getattr(exc, "code", "") == "duplicate" else status.HTTP_422_UNPROCESSABLE_CONTENT
    return HTTPException(code, exc.reason)


# ==================================================================== labs ===
class LabSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    training_pooling_consent: bool


def _summary(tenant: Tenant) -> LabSummary:
    return LabSummary(id=tenant.id, name=tenant.name, slug=tenant.slug, status=tenant.status, training_pooling_consent=tenant.training_pooling_consent)


@router.get("/labs", response_model=list[LabSummary])
def list_labs(admin: CurrentAdmin, response: Response, page: int | None = None, page_size: int | None = None) -> list[LabSummary]:
    """Every lab, in every status, a page at a time."""
    with read_session() as session:
        paged = paginate(session, select(Tenant).order_by(Tenant.name, Tenant.id), Page.of(page, page_size))
        set_page_headers(response, paged, "/admin/api/labs")
        return [_summary(t) for t in paged.rows]


class RegisterLabRequest(BaseModel):
    name: str = Field(min_length=1)
    slug: str = Field(pattern=SLUG_PATTERN)
    admin_email: str = Field(min_length=3)
    admin_display_name: str = Field(min_length=1)
    admin_employee_code: str = Field(min_length=1)
    training_pooling_consent: bool = False
    training_consent_ref: str | None = None
    patient_notice_version: str | None = None


@router.post("/labs", response_model=LabSummary, status_code=status.HTTP_201_CREATED)
def register_lab_json(body: RegisterLabRequest, admin: CurrentAdmin) -> LabSummary:
    """Register a lab and its first lab admin."""
    with system_session() as session:
        try:
            result = register_lab(session, LabRegistration(**body.model_dump()), actor_id=admin.platform_user_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return _summary(result.tenant)


class ReadinessResponse(BaseModel):
    passed: bool
    checks: list[dict]


@router.get("/labs/{tenant_id}/readiness", response_model=ReadinessResponse)
def readiness(tenant_id: uuid.UUID, session: AdminLabDb) -> ReadinessResponse:
    """The checks gating the move to pilot, evaluated now and not recorded."""
    report = evaluate_readiness(session, tenant_id, persist=False)
    return ReadinessResponse(passed=report.passed, checks=[{"check_id": o.check_id, "status": o.status, "measured_value": o.measured_value, "threshold": o.threshold, "detail": o.detail} for o in report.outcomes])


class StatusChangeRequest(BaseModel):
    status: str


@router.post("/labs/{tenant_id}/status", response_model=LabSummary)
def change_status(tenant_id: uuid.UUID, body: StatusChangeRequest, session: AdminLabDb, admin: CurrentAdmin) -> LabSummary:
    """Move a lab through its lifecycle, through the gates."""
    try:
        tenant = transition_status(session, tenant_id, body.status, actor_id=admin.platform_user_id)
    except TenantTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _summary(tenant)


# ========================================================= models per step ===
@router.get("/labs/{tenant_id}/steps")
def steps(tenant_id: uuid.UUID, session: AdminLabDb) -> list[dict[str, Any]]:
    """Each pipeline step and what serves it."""
    return [{"task_key": s.task_key, "task_bucket": s.task_bucket, "is_asr": s.is_asr, "active_model": s.active_model, "active_provider": s.active_provider, "is_local": s.is_local, "is_configured": s.is_configured, "proposed": [{"assignment_id": str(aid), "label": label} for aid, label in s.proposed]} for s in step_configuration(session, tenant_id=tenant_id)]


class ProposeRequest(BaseModel):
    task_key: str
    model_definition_id: uuid.UUID


@router.post("/labs/{tenant_id}/assignments", status_code=status.HTTP_201_CREATED)
def propose(tenant_id: uuid.UUID, body: ProposeRequest, session: AdminLabDb, admin: CurrentAdmin) -> dict[str, str]:
    """Propose a model for one step; it does not go live."""
    try:
        assignment = propose_assignment(session, tenant_id=tenant_id, task_key=body.task_key, model_definition_id=body.model_definition_id, actor_id=admin.platform_user_id)
    except ConfigRefused as exc:
        raise _refused(exc) from exc
    return {"assignment_id": str(assignment.id), "status": assignment.status}


@router.post("/labs/{tenant_id}/assignments/{assignment_id}/activate")
def activate(tenant_id: uuid.UUID, assignment_id: uuid.UUID, session: AdminLabDb, admin: CurrentAdmin) -> dict[str, str]:
    """Make a proposed assignment live, through the eval gate."""
    try:
        assignment = activate_assignment(session, assignment_id=assignment_id, tenant_id=tenant_id, actor_id=admin.platform_user_id)
    except UngatedActivation as exc:
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, str(exc)) from exc
    except ModelResolutionError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"assignment_id": str(assignment.id), "status": assignment.status}


# ============================================================== onboarding ===
@router.get("/labs/{tenant_id}/onboarding")
def onboarding_status(tenant_id: uuid.UUID, session: AdminLabDb) -> dict[str, Any]:
    """Where this lab is across every onboarding stage."""
    return onboarding_steps.onboarding_overview(session, tenant_id)


@router.get("/labs/{tenant_id}/onboarding/batches")
def list_batches(tenant_id: uuid.UUID, session: AdminLabDb, response: Response, page: int | None = None, page_size: int | None = None, batch_type: str | None = None) -> list[dict[str, Any]]:
    """Every import batch for the lab, newest first, a page at a time."""
    paged = paginate(session, onboarding_steps.batches_query(tenant_id, batch_type=batch_type), Page.of(page, page_size))
    set_page_headers(response, paged, f"/admin/api/labs/{tenant_id}/onboarding/batches", {"batch_type": batch_type} if batch_type else None)
    return [onboarding_steps.batch_summary(b) for b in paged.rows]


@router.get("/labs/{tenant_id}/onboarding/batches/{batch_id}")
def batch_status(tenant_id: uuid.UUID, batch_id: uuid.UUID, session: AdminLabDb) -> dict[str, Any]:
    """One batch: where it is, what it holds, and what still blocks it."""
    try:
        return onboarding_steps.batch_status(session, tenant_id, batch_id)
    except StepRefused as exc:
        raise _refused(exc) from exc


@router.post("/labs/{tenant_id}/onboarding/roster")
async def upload_roster(tenant_id: uuid.UUID, session: AdminLabDb, file: Annotated[UploadFile, File()], trigger: Annotated[str, Form()] = ImportTrigger.INITIAL_ONBOARDING) -> dict[str, Any]:
    """Import the lab's roster from an HR CSV export."""
    try:
        return onboarding_steps.import_roster_file(session, tenant_id, await file.read(), trigger=trigger)
    except StepRefused as exc:
        raise _refused(exc) from exc


@router.post("/labs/{tenant_id}/onboarding/templates")
async def upload_templates(tenant_id: uuid.UUID, session: AdminLabDb, files: Annotated[list[UploadFile], File()], trigger: Annotated[str, Form()] = ImportTrigger.INITIAL_ONBOARDING) -> dict[str, Any]:
    """Parse uploaded template documents into candidates for radiologist review."""
    uploads = [ArtifactUpload(filename=f.filename or "unnamed", data=await f.read(), mime_type=f.content_type) for f in files]
    try:
        return onboarding_steps.submit_template_files(session, tenant_id, uploads, trigger=trigger)
    except StepRefused as exc:
        raise _refused(exc) from exc


class CorpusRecordIn(BaseModel):
    report_text: str
    external_report_id: str | None = None
    radiologist_employee_code: str | None = None
    referring_doctor: str | None = None
    patient_sex: str | None = None
    patient_age_years: int | None = None
    is_deidentified: bool = False


class CorpusLoadRequest(BaseModel):
    records: list[CorpusRecordIn]
    trigger: str = ImportTrigger.INITIAL_ONBOARDING


@router.post("/labs/{tenant_id}/onboarding/shorthand", status_code=status.HTTP_201_CREATED)
async def upload_shorthand(tenant_id: uuid.UUID, session: AdminLabDb, files: Annotated[list[UploadFile], File()]) -> dict[str, Any]:
    """Shorthand reference sheets (PDF, Word or text) into the lab's lexicon."""
    uploads = [ArtifactUpload(filename=f.filename or "unnamed", data=await f.read(), mime_type=f.content_type) for f in files]
    try:
        return onboarding_steps.submit_shorthand_files(session, tenant_id, uploads)
    except StepRefused as exc:
        raise _refused(exc) from exc


@router.post("/labs/{tenant_id}/onboarding/corpus")
def upload_corpus(tenant_id: uuid.UUID, body: CorpusLoadRequest, session: AdminLabDb) -> dict[str, Any]:
    """Bulk-load historical signed reports."""
    return onboarding_steps.load_corpus_records(session, tenant_id, [corpus.CorpusRecord(**r.model_dump()) for r in body.records], trigger=body.trigger)


@router.post("/labs/{tenant_id}/onboarding/batches/{batch_id}/merge-proposals")
def merge_proposals(tenant_id: uuid.UUID, batch_id: uuid.UUID, session: AdminLabDb) -> dict[str, Any]:
    """Propose merges for near-duplicate templates in one batch."""
    try:
        return onboarding_steps.propose_template_merges(session, tenant_id, batch_id)
    except StepRefused as exc:
        raise _refused(exc) from exc


@router.post("/labs/{tenant_id}/onboarding/steps/{step}")
def run_onboarding_step(tenant_id: uuid.UUID, step: str, session: AdminLabDb, options: Annotated[dict[str, Any] | None, Body()] = None) -> dict[str, Any]:
    """Run one mining or seeding step by name; see `onboarding_steps.STEPS`."""
    try:
        return onboarding_steps.run_step(session, tenant_id, step, options)
    except StepRefused as exc:
        raise _refused(exc) from exc


# ======================================================= lab users' sign-in ===
class LabUserOut(BaseModel):
    id: uuid.UUID
    employee_code: str
    display_name: str
    email: str | None
    roles: list[str]
    is_active: bool
    can_sign_in: bool
    last_login_at: str | None


def lab_user_out(user: AppUser) -> LabUserOut:
    return LabUserOut(id=user.id, employee_code=user.employee_code, display_name=user.display_name, email=user.email, roles=list(user.roles or ()), is_active=bool(user.is_active), can_sign_in=bool(user.password_hash and user.email and user.is_active), last_login_at=user.last_login_at.isoformat() if user.last_login_at else None)


@router.get("/labs/{tenant_id}/users", response_model=list[LabUserOut])
def list_lab_users(tenant_id: uuid.UUID, session: AdminLabDb, response: Response, page: int | None = None, page_size: int | None = None) -> list[LabUserOut]:
    """The lab's staff accounts and whether each can sign in, a page at a time."""
    paged = paginate(session, select(AppUser).where(AppUser.tenant_id == tenant_id).order_by(AppUser.display_name, AppUser.id), Page.of(page, page_size))
    set_page_headers(response, paged, f"/admin/api/labs/{tenant_id}/users")
    return [lab_user_out(u) for u in paged.rows]


class LabPasswordRequest(BaseModel):
    password: str


@router.post("/labs/{tenant_id}/users/{user_id}/password", response_model=LabUserOut)
def set_lab_user_password(tenant_id: uuid.UUID, user_id: uuid.UUID, body: LabPasswordRequest, session: AdminLabDb, admin: CurrentAdmin) -> LabUserOut:
    """Set a lab user's password and sign them out everywhere."""
    if len(body.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"a password must be at least {MIN_PASSWORD_LENGTH} characters")
    user = session.get(AppUser, user_id)
    if user is None or user.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no lab user {user_id}")
    try:
        return lab_user_out(lab_auth.set_password(session, user_id=user_id, password=body.password, actor_id=admin.platform_user_id, actor_is_platform=True))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


# =================================================== autonomy, adaptation ===
@router.get("/labs/{tenant_id}/autonomy/{class_code}")
def get_accrual(tenant_id: uuid.UUID, class_code: str, session: AdminLabDb) -> dict[str, Any]:
    """Current evidence for an autonomy class; grants nothing."""
    try:
        s = accrual.snapshot(session, tenant_id=tenant_id, class_code=class_code)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"class_code": s.class_code, "baseline_cse_rate": s.baseline_cse_rate, "graded_n": s.graded_n, "required_n": s.required_n, "observed_cse": s.observed_cse, "observed_rate": s.observed_rate, "posterior_prob_ni": s.posterior_prob_ni, "meets_volume": s.meets_volume, "would_support_grant": s.would_support_grant}


@router.post("/labs/{tenant_id}/autonomy/{class_code}/open-accrual")
def open_accrual(tenant_id: uuid.UUID, class_code: str, session: AdminLabDb, admin: CurrentAdmin) -> dict[str, str]:
    """Start collecting evidence for a class."""
    try:
        klass = accrual.open_accrual(session, tenant_id=tenant_id, class_code=class_code, actor_id=admin.platform_user_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"class_code": klass.code, "status": klass.status}


@router.post("/labs/{tenant_id}/autonomy/{class_code}/grant")
def grant_autonomy(tenant_id: uuid.UUID, class_code: str, session: AdminLabDb, admin: CurrentAdmin) -> dict[str, Any]:
    """Grant autonomy to a class whose evidence supports it."""
    try:
        klass = grant.grant(session, tenant_id=tenant_id, class_code=class_code, platform_user_id=admin.platform_user_id)
    except GrantRefused as exc:
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"class_code": klass.code, "status": klass.status, "granted_at": klass.granted_at.isoformat() if klass.granted_at else None, "cusum_threshold": float(klass.cusum_threshold)}


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=1)


@router.post("/labs/{tenant_id}/autonomy/{class_code}/revoke")
def revoke_autonomy(tenant_id: uuid.UUID, class_code: str, body: RevokeRequest, session: AdminLabDb, admin: CurrentAdmin) -> dict[str, str]:
    """Withdraw autonomy; the lab can also do this from its side."""
    try:
        klass = grant.revoke(session, tenant_id=tenant_id, class_code=class_code, reason=body.reason, actor_id=admin.platform_user_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"class_code": klass.code, "status": klass.status, "reason": body.reason}


class GatesRequest(BaseModel):
    recording_ids: list[uuid.UUID]
    target: str = AdaptationTarget.ASR_GLOBAL


@router.post("/labs/{tenant_id}/adaptation/gates")
def adaptation_gates(tenant_id: uuid.UUID, body: GatesRequest, session: AdminLabDb) -> dict[str, Any]:
    """Evaluate the six gates over a candidate training corpus."""
    report = evaluate_gates(session, recording_ids=body.recording_ids, target=body.target)
    payload = report.as_json()
    payload["blocked_by"] = [r.gate_id for r in report.failures]
    return payload


@router.post("/labs/{tenant_id}/adaptation/require-gates")
def require_adaptation_gates(tenant_id: uuid.UUID, body: GatesRequest, session: AdminLabDb) -> dict[str, Any]:
    """The raising form, for a training entry point."""
    try:
        report = require_gates(session, recording_ids=body.recording_ids, target=body.target)
    except AdaptationBlocked as exc:
        raise HTTPException(status.HTTP_412_PRECONDITION_FAILED, str(exc)) from exc
    return report.as_json()


# ========================================================== platform users ===
class PlatformUserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: str
    is_active: bool
    last_login_at: str | None


def _user_out(user: PlatformUser) -> PlatformUserOut:
    return PlatformUserOut(id=user.id, email=user.email, display_name=user.display_name, role=user.role, is_active=bool(user.is_active), last_login_at=user.last_login_at.isoformat() if user.last_login_at else None)


@router.get("/users", response_model=list[PlatformUserOut])
def list_users(admin: CurrentAdmin, response: Response, page: int | None = None, page_size: int | None = None) -> list[PlatformUserOut]:
    """Every product admin and support account, a page at a time."""
    with read_session() as session:
        paged = paginate(session, users.platform_users_query(), Page.of(page, page_size))
        set_page_headers(response, paged, "/admin/api/users")
        return [_user_out(u) for u in paged.rows]


class CreateUserRequest(BaseModel):
    email: str
    display_name: str
    role: str
    password: str


@router.post("/users", response_model=PlatformUserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: CreateUserRequest, admin: CurrentAdmin) -> PlatformUserOut:
    """Add a product admin or support account."""
    with system_session() as session:
        try:
            return _user_out(users.create_platform_user(session, **body.model_dump(), actor_id=admin.platform_user_id))
        except users.UserChangeRefused as exc:
            raise _refused(exc) from exc


def _set_active(user_id: uuid.UUID, active: bool, admin: CurrentAdmin) -> PlatformUserOut:
    with system_session() as session:
        try:
            return _user_out(users.set_active(session, user_id=user_id, active=active, actor_id=admin.platform_user_id))
        except users.UserChangeRefused as exc:
            raise _refused(exc) from exc


@router.post("/users/{user_id}/deactivate", response_model=PlatformUserOut)
def deactivate_user(user_id: uuid.UUID, admin: CurrentAdmin) -> PlatformUserOut:
    """Switch an account off and end its sessions."""
    return _set_active(user_id, False, admin)


@router.post("/users/{user_id}/reactivate", response_model=PlatformUserOut)
def reactivate_user(user_id: uuid.UUID, admin: CurrentAdmin) -> PlatformUserOut:
    """Switch an account back on."""
    return _set_active(user_id, True, admin)


class PasswordRequest(BaseModel):
    password: str


@router.post("/users/{user_id}/password", response_model=PlatformUserOut)
def reset_user_password(user_id: uuid.UUID, body: PasswordRequest, admin: CurrentAdmin) -> PlatformUserOut:
    """Replace an account's password and sign it out everywhere."""
    with system_session() as session:
        try:
            return _user_out(users.reset_password(session, user_id=user_id, password=body.password, actor_id=admin.platform_user_id))
        except users.UserChangeRefused as exc:
            raise _refused(exc) from exc


class OwnPasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/account/password", status_code=status.HTTP_204_NO_CONTENT)
def change_own_password(body: OwnPasswordRequest, admin: CurrentAdmin) -> Response:
    """Replace your own password; every session, this one included, ends."""
    with system_session() as session:
        try:
            users.change_own_password(session, user_id=admin.platform_user_id, current=body.current_password, new=body.new_password)
        except users.UserChangeRefused as exc:
            code = status.HTTP_403_FORBIDDEN if exc.code == "wrong_password" else status.HTTP_422_UNPROCESSABLE_CONTENT
            raise HTTPException(code, exc.reason) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
