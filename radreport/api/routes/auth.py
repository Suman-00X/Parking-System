"""Lab users' sign-in endpoints: trade a password for tokens, refresh them, sign out, change a password.

Order: sign in (login) -> keep the sign-in alive (refresh_tokens) -> end it (logout) -> replace
your own password (change_own_password).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from radreport.api.deps import CurrentPrincipal, DbSession, client_ip
from radreport.auth import lab
from radreport.auth.lab import SignInFailed, TokenInvalid
from radreport.core.types import TenantStatus
from radreport.db.models.tenancy import Tenant
from radreport.db.session import system_session, tenant_session

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str


def _response(pair: lab.TokenPair) -> TokenResponse:
    return TokenResponse(access_token=pair.access_token, expires_in=pair.access_expires_in, refresh_token=pair.refresh_token)


class LoginRequest(BaseModel):
    lab: str
    email: str
    password: str


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request) -> TokenResponse:
    """Sign a lab user in with their lab's slug, email and password."""
    refused = HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid lab, email or password")
    with system_session() as session:
        tenant = session.execute(select(Tenant).where(Tenant.slug == body.lab)).scalar_one_or_none()
        tenant_id = tenant.id if tenant and tenant.status != TenantStatus.OFFBOARDED else None
    if tenant_id is None:
        # Same answer as a wrong password, so the form cannot be used to list labs.
        raise refused
    with tenant_session(tenant_id) as session:
        try:
            return _response(lab.login(session, tenant_id=tenant_id, email=body.email, password=body.password, user_agent=request.headers.get("user-agent"), ip_address=client_ip(request)))
        except SignInFailed as exc:
            raise refused from exc


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=TokenResponse)
def refresh_tokens(body: RefreshRequest, request: Request) -> TokenResponse:
    """Trade a refresh token for a new access token and refresh token."""
    try:
        tenant_id = lab.tenant_of_refresh_token(body.refresh_token)
    except TokenInvalid as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    refused: TokenInvalid | None = None
    with tenant_session(tenant_id) as session:
        try:
            pair = lab.refresh(session, raw=body.refresh_token, user_agent=request.headers.get("user-agent"), ip_address=client_ip(request))
        except TokenInvalid as exc:
            # Caught inside the transaction on purpose: a refusal may have revoked a stolen token's
            # whole sign-in, and raising here would roll that revocation back.
            refused = exc
    if refused is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(refused)) from refused
    return _response(pair)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: RefreshRequest) -> Response:
    """End the sign-in a refresh token belongs to; harmless if it is unknown."""
    try:
        tenant_id = lab.tenant_of_refresh_token(body.refresh_token)
    except TokenInvalid:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    with tenant_session(tenant_id) as session:
        lab.logout(session, raw=body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_own_password(body: ChangePasswordRequest, session: DbSession, principal: CurrentPrincipal) -> Response:
    """Replace your own password; every sign-in, this one included, ends."""
    try:
        lab.change_password(session, user_id=principal.id, current=body.current_password, new=body.new_password)
    except SignInFailed as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
