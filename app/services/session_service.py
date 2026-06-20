"""Refresh-session registry: issue, rotate, revoke.

Each login creates a `user_sessions` row keyed by the refresh token's `jti`.
We store only a SHA-256 of the refresh token (never the token itself). Rotation
issues a fresh token and revokes the old row; presenting an already-revoked
token is treated as theft → the whole subject's sessions are revoked.

All queries run under the subject's tenant context (RLS scopes `user_sessions`
to the current subject), so the caller must set the context before invoking.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import session_scope
from app.security.context import TenantContext
from app.security.tokens import (
    AccessClaims,
    access_claims_from_payload,
    create_access_token,
    create_refresh_token,
    decode_token,
    REFRESH,
)


class SessionError(Exception):
    """Refresh failed (unknown / expired / revoked / reused token)."""


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    session_id: uuid.UUID


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create_session(
    db: AsyncSession,
    claims: AccessClaims,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
) -> IssuedTokens:
    """Mint a new session + access/refresh pair for an authenticated subject."""
    session_id = uuid.uuid4()
    refresh_token = create_refresh_token(session_id, claims)
    expires_at = _now() + timedelta(seconds=settings.refresh_token_ttl_seconds)

    await db.execute(
        text(
            "INSERT INTO user_sessions (id, organization_id, subject_type, subject_id, "
            "refresh_token_hash, user_agent, ip, expires_at) "
            "VALUES (:id, :org, :stype, :sid, :hash, :ua, :ip, :exp)"
        ),
        {
            "id": session_id,
            "org": claims.organization_id,
            "stype": claims.subject_type,
            "sid": claims.subject_id,
            "hash": _hash(refresh_token),
            "ua": user_agent,
            "ip": ip,
            "exp": expires_at,
        },
    )
    return IssuedTokens(
        access_token=create_access_token(claims),
        refresh_token=refresh_token,
        session_id=session_id,
    )


async def rotate_session(
    db: AsyncSession,
    presented_refresh: str,
    *,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[AccessClaims, IssuedTokens]:
    """Validate + rotate a refresh token. Returns (claims, new tokens).

    The caller must already have set the tenant context from the token's claims
    so RLS exposes the matching session row.
    """
    payload = decode_token(presented_refresh, expected_use=REFRESH)
    claims = access_claims_from_payload(payload)
    jti = uuid.UUID(payload["jti"])

    row = (
        await db.execute(
            text(
                "SELECT refresh_token_hash, revoked_at, expires_at "
                "FROM user_sessions WHERE id = :id"
            ),
            {"id": jti},
        )
    ).first()

    if row is None:
        raise SessionError("unknown session")

    # Reuse of a revoked token => likely theft: revoke the whole subject chain.
    # Done in an independent transaction so it survives the caller's rollback.
    if row.revoked_at is not None:
        await _revoke_all_committed(claims)
        raise SessionError("refresh token reuse detected")

    if row.expires_at <= _now():
        raise SessionError("session expired")
    if row.refresh_token_hash != _hash(presented_refresh):
        # Hash mismatch on a live session => also treat as compromise.
        await _revoke_all_committed(claims)
        raise SessionError("refresh token mismatch")

    # Revoke the old row and mint a fresh session.
    await db.execute(
        text("UPDATE user_sessions SET revoked_at = now() WHERE id = :id"), {"id": jti}
    )
    issued = await create_session(db, claims, user_agent=user_agent, ip=ip)
    return claims, issued


async def revoke_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(
        text(
            "UPDATE user_sessions SET revoked_at = now() "
            "WHERE id = :id AND revoked_at IS NULL"
        ),
        {"id": session_id},
    )


async def revoke_all(db: AsyncSession, subject_type: str, subject_id: uuid.UUID) -> None:
    await db.execute(
        text(
            "UPDATE user_sessions SET revoked_at = now() "
            "WHERE subject_type = :stype AND subject_id = :sid AND revoked_at IS NULL"
        ),
        {"stype": subject_type, "sid": subject_id},
    )


async def _revoke_all_committed(claims: AccessClaims) -> None:
    """Revoke the subject's sessions in a standalone, committed transaction so the
    security action persists even when the caller's request transaction rolls back
    (e.g. the refresh endpoint then returns 401)."""
    ctx = TenantContext(
        organization_id=claims.organization_id,
        subject_type=claims.subject_type,
        subject_id=claims.subject_id,
        role=claims.role,
    )
    async with session_scope(ctx) as fresh:
        await revoke_all(fresh, claims.subject_type, claims.subject_id)
