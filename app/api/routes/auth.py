"""Authentication endpoints (admin + client), firm-branded and org-scoped.

Pre-auth lookups/writes run under a system context pinned to the resolved org
(RLS still enforces firm isolation). Session creation runs under the subject's
own context so the user_sessions RLS check passes. Responses are uniform to
resist account enumeration.
"""

from __future__ import annotations

import uuid

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.deps import get_access_claims, require_csrf, require_org
from app.database import session_scope
from app.schemas.auth import (
    ClientSignupIn,
    LoginIn,
    MeOut,
    MessageOut,
    OtpRequestIn,
    OtpVerifyIn,
    OtpVerifyOut,
    SessionOut,
)
from app.security.context import TenantContext, system_context
from app.security.cookies import (
    REFRESH_COOKIE,
    clear_auth_cookies,
    set_auth_cookies,
    set_csrf_cookie,
)
from app.security.csrf import CSRF_COOKIE_NAME, generate_csrf_token
from app.security.passwords import hash_password, verify_password
from app.security.tokens import (
    REFRESH,
    SIGNUP,
    AccessClaims,
    access_claims_from_payload,
    create_signup_token,
    decode_token,
)
from app.services import auth_service, otp_service, session_service
from app.services.rate_limit import RateLimitExceeded, guard_login_attempt, guard_otp_request

router = APIRouter(prefix="/auth", tags=["auth"])

# Precomputed hash so login verifies *something* even when no account matches,
# equalizing timing to resist enumeration.
_DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


async def _issue_session(response: Response, claims: AccessClaims, request: Request) -> str:
    """Create the session under the subject's own context, set cookies, and
    return the CSRF token (also echoed in the body for split-origin SPAs)."""
    ctx = TenantContext(claims.organization_id, claims.subject_type, claims.subject_id, claims.role)
    async with session_scope(ctx) as db:
        issued = await session_service.create_session(
            db, claims, user_agent=request.headers.get("user-agent"), ip=_client_ip(request)
        )
    csrf_token = generate_csrf_token()
    set_auth_cookies(
        response,
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        csrf_token=csrf_token,
    )
    return csrf_token


@router.post("/otp/request", response_model=MessageOut)
async def otp_request(
    body: OtpRequestIn, request: Request, org: uuid.UUID = Depends(require_org)
) -> MessageOut:
    phone = auth_service.normalize_phone(body.phone)
    try:
        guard_otp_request(phone, _client_ip(request))
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests",
                            headers={"Retry-After": str(e.retry_after_seconds)})
    try:
        await otp_service.start_verification(phone)
    except otp_service.OtpNotConfigured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OTP unavailable")
    except otp_service.OtpError:
        pass  # uniform response — never reveal delivery/account details
    return MessageOut(status="sent")


@router.post("/otp/verify", response_model=OtpVerifyOut)
async def otp_verify(
    body: OtpVerifyIn, request: Request, response: Response, org: uuid.UUID = Depends(require_org)
) -> OtpVerifyOut:
    phone = auth_service.normalize_phone(body.phone)
    if not await otp_service.check_verification(phone, body.code):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid code")

    claims: AccessClaims | None = None
    async with session_scope(system_context(org)) as db:
        admin = await auth_service.find_admin_by_phone(db, org, phone)
        if admin and admin.is_active:
            await auth_service.touch_last_login(db, "users", admin.id)
            await auth_service.record_audit(db, org, actor_type="user", actor_id=admin.id, action="login.otp")
            claims = AccessClaims(org, "user", admin.id, admin.role)
        else:
            client = await auth_service.find_client_by_phone(db, org, phone)
            if client and client.is_active:
                await auth_service.touch_last_login(db, "client_accounts", client.id)
                await auth_service.record_audit(db, org, actor_type="client", actor_id=client.id, action="login.otp")
                claims = AccessClaims(org, "client", client.id, None)
            else:
                lead_id = await auth_service.find_claimable_lead_by_phone(db, org, phone)
                if lead_id:
                    account_id = await auth_service.create_client_account_for_lead(db, org, lead_id, phone=phone)
                    await auth_service.record_audit(db, org, actor_type="client", actor_id=account_id, action="claim")
                    claims = AccessClaims(org, "client", account_id, None)

    if claims is None:
        # Phone proven, but no account/lead → let them sign up with a short token.
        return OtpVerifyOut(authenticated=False, signup_token=create_signup_token(org, phone))

    csrf = await _issue_session(response, claims, request)
    return OtpVerifyOut(
        authenticated=True, subject_type=claims.subject_type, role=claims.role, csrf_token=csrf
    )


@router.post("/login", response_model=SessionOut)
async def login(
    body: LoginIn, request: Request, response: Response, org: uuid.UUID = Depends(require_org)
) -> SessionOut:
    email = auth_service.normalize_email(body.email)
    try:
        guard_login_attempt(email, _client_ip(request))
    except RateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests",
                            headers={"Retry-After": str(e.retry_after_seconds)})

    claims: AccessClaims | None = None
    async with session_scope(system_context(org)) as db:
        admin = await auth_service.find_admin_by_email(db, org, email)
        if admin and admin.is_active and admin.password_hash and verify_password(body.password, admin.password_hash):
            await auth_service.touch_last_login(db, "users", admin.id)
            await auth_service.record_audit(db, org, actor_type="user", actor_id=admin.id, action="login.password")
            claims = AccessClaims(org, "user", admin.id, admin.role)
        else:
            client = await auth_service.find_client_by_email(db, org, email)
            if client and client.is_active and client.password_hash and verify_password(body.password, client.password_hash):
                await auth_service.touch_last_login(db, "client_accounts", client.id)
                await auth_service.record_audit(db, org, actor_type="client", actor_id=client.id, action="login.password")
                claims = AccessClaims(org, "client", client.id, None)
            else:
                verify_password(body.password, _DUMMY_HASH)  # equalize timing

    if claims is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    csrf = await _issue_session(response, claims, request)
    return SessionOut(subject_type=claims.subject_type, role=claims.role, csrf_token=csrf)


@router.post("/client/signup", response_model=SessionOut)
async def client_signup(
    body: ClientSignupIn, request: Request, response: Response, org: uuid.UUID = Depends(require_org)
) -> SessionOut:
    if not body.valid_case_type():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid case type")
    try:
        payload = decode_token(body.signup_token, expected_use=SIGNUP)
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signup token")
    if payload.get("org") != str(org):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signup token")

    phone = payload["phone"]  # normalized when the OTP was verified
    email = auth_service.normalize_email(body.email)
    password_hash = hash_password(body.password)

    async with session_scope(system_context(org)) as db:
        if await auth_service.find_client_by_phone(db, org, phone) or await auth_service.find_client_by_email(db, org, email):
            raise HTTPException(status.HTTP_409_CONFLICT, "Account already exists")
        account_id, _lead_id = await auth_service.create_client_signup(
            db, org,
            full_name=body.full_name, email=email, phone=phone, password_hash=password_hash,
            case_type=body.case_type, description=body.incident_description,
            location=body.incident_location, injury_area=body.injury_area,
        )
        await auth_service.record_audit(db, org, actor_type="client", actor_id=account_id, action="signup")

    claims = AccessClaims(org, "client", account_id, None)
    csrf = await _issue_session(response, claims, request)
    return SessionOut(subject_type="client", role=None, csrf_token=csrf)


@router.post("/refresh", response_model=MessageOut)
async def refresh(request: Request, response: Response, _: None = Depends(require_csrf)) -> MessageOut:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        claims = access_claims_from_payload(decode_token(token, expected_use=REFRESH))
    except (jwt.InvalidTokenError, KeyError, ValueError):
        clear_auth_cookies(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session")

    ctx = TenantContext(claims.organization_id, claims.subject_type, claims.subject_id, claims.role)
    try:
        async with session_scope(ctx) as db:
            _, issued = await session_service.rotate_session(
                db, token, user_agent=request.headers.get("user-agent"), ip=_client_ip(request)
            )
    except session_service.SessionError:
        clear_auth_cookies(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session invalid")

    csrf = generate_csrf_token()
    set_auth_cookies(
        response,
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        csrf_token=csrf,
    )
    return MessageOut(status="refreshed", csrf_token=csrf)


@router.post("/logout", response_model=MessageOut)
async def logout(
    request: Request, response: Response,
    claims: AccessClaims = Depends(get_access_claims), _: None = Depends(require_csrf),
) -> MessageOut:
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        try:
            jti = uuid.UUID(decode_token(token, expected_use=REFRESH)["jti"])
        except (jwt.InvalidTokenError, KeyError, ValueError):
            jti = None
        if jti is not None:
            ctx = TenantContext(claims.organization_id, claims.subject_type, claims.subject_id, claims.role)
            async with session_scope(ctx) as db:
                await session_service.revoke_session(db, jti)
    clear_auth_cookies(response)
    return MessageOut(status="logged_out")


@router.post("/logout-all", response_model=MessageOut)
async def logout_all(
    response: Response,
    claims: AccessClaims = Depends(get_access_claims), _: None = Depends(require_csrf),
) -> MessageOut:
    ctx = TenantContext(claims.organization_id, claims.subject_type, claims.subject_id, claims.role)
    async with session_scope(ctx) as db:
        await session_service.revoke_all(db, claims.subject_type, claims.subject_id)
    clear_auth_cookies(response)
    return MessageOut(status="logged_out_all")


@router.get("/csrf", response_model=MessageOut)
async def csrf(request: Request, response: Response) -> MessageOut:
    """Return the current CSRF token (issuing one if absent) so a freshly loaded
    SPA can echo it on state-changing requests. Safe: only our origins can read
    the response (CORS), so an attacker can't learn the token."""
    token = request.cookies.get(CSRF_COOKIE_NAME)
    if not token:
        token = generate_csrf_token()
        set_csrf_cookie(response, token)
    return MessageOut(status="ok", csrf_token=token)


@router.get("/me", response_model=MeOut)
async def me(claims: AccessClaims = Depends(get_access_claims)) -> MeOut:
    return MeOut(
        organization_id=str(claims.organization_id),
        subject_type=claims.subject_type,
        subject_id=str(claims.subject_id),
        role=claims.role,
    )
