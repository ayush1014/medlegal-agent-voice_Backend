"""Request dependencies: firm-branded org resolution, auth context → RLS, CSRF.

These are the chokepoint described in the PRD §8.6 — every authenticated request
turns a JWT cookie into a TenantContext and runs its DB work inside a
context-stamped transaction, so Postgres RLS enforces isolation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import session_scope
from app.security.cookies import ACCESS_COOKIE
from app.security.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, verify_csrf
from app.security.tokens import ACCESS, AccessClaims, access_claims_from_payload, decode_token
from app.security.context import TenantContext
from app.services.org_service import resolve_org_id


def _slug_from_request(request: Request) -> str | None:
    """Firm slug from the dev header, else the host subdomain in prod."""
    header_slug = request.headers.get("X-Org-Slug")
    if header_slug:
        return header_slug.strip().lower()
    if settings.base_domain:
        host = (request.headers.get("host") or "").split(":")[0].lower()
        suffix = "." + settings.base_domain.lower()
        if host.endswith(suffix):
            sub = host[: -len(suffix)]
            # Ignore bare apex / www.
            if sub and sub != "www":
                return sub
    return None


async def require_org(request: Request) -> uuid.UUID:
    """Resolve the firm before credentials. Fails closed if unknown (no global
    login surface)."""
    slug = _slug_from_request(request)
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown firm")
    # Pre-auth lookup via the SECURITY DEFINER resolver (no tenant context yet).
    async with session_scope(None) as db:
        org_id = await resolve_org_id(db, slug)
    if org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown firm")
    return org_id


def get_access_claims(request: Request) -> AccessClaims:
    """Decode the access-token cookie into identity claims, or 401."""
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = decode_token(token, expected_use=ACCESS)
        return access_claims_from_payload(payload)
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session")


def require_csrf(request: Request) -> None:
    """Double-submit CSRF check for state-changing requests."""
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    header = request.headers.get(CSRF_HEADER_NAME)
    if not verify_csrf(cookie, header):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF check failed")


async def get_authed_db(
    claims: AccessClaims = Depends(get_access_claims),
) -> AsyncGenerator[AsyncSession, None]:
    """A request-scoped session stamped with the caller's tenant context."""
    ctx = TenantContext(
        organization_id=claims.organization_id,
        subject_type=claims.subject_type,
        subject_id=claims.subject_id,
        role=claims.role,
    )
    async with session_scope(ctx) as session:
        yield session


async def get_staff_db(
    claims: AccessClaims = Depends(get_access_claims),
) -> AsyncGenerator[AsyncSession, None]:
    """Firm-staff-only session (lead management). Clients are 403'd."""
    if claims.subject_type != "user":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff only")
    ctx = TenantContext(
        organization_id=claims.organization_id,
        subject_type=claims.subject_type,
        subject_id=claims.subject_id,
        role=claims.role,
    )
    async with session_scope(ctx) as session:
        yield session


async def get_client_db(
    claims: AccessClaims = Depends(get_access_claims),
) -> AsyncGenerator[AsyncSession, None]:
    """Client-only session (the patient portal). Staff are 403'd."""
    if claims.subject_type != "client":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Clients only")
    ctx = TenantContext(
        organization_id=claims.organization_id,
        subject_type=claims.subject_type,
        subject_id=claims.subject_id,
        role=claims.role,
    )
    async with session_scope(ctx) as session:
        yield session
