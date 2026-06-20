"""Session JWTs (access + refresh).

Access tokens carry the tenant identity that maps to the RLS GUCs; refresh
tokens carry only a session id (`jti`) so they can be rotated/revoked via the
`user_sessions` registry. Both are signed HS256 with `settings.jwt_secret` and
delivered in HttpOnly cookies (never localStorage).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings

ACCESS = "access"
REFRESH = "refresh"
SIGNUP = "signup"


@dataclass(frozen=True)
class AccessClaims:
    organization_id: uuid.UUID
    subject_type: str  # "user" | "client"
    subject_id: uuid.UUID
    role: str | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(payload: dict, ttl_seconds: int) -> str:
    now = _now()
    payload = {**payload, "iat": now, "exp": now + timedelta(seconds=ttl_seconds)}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(claims: AccessClaims) -> str:
    return _encode(
        {
            "token_use": ACCESS,
            "org": str(claims.organization_id),
            "sub_type": claims.subject_type,
            "sub": str(claims.subject_id),
            "role": claims.role,
        },
        settings.access_token_ttl_seconds,
    )


def create_refresh_token(session_id: uuid.UUID, claims: AccessClaims) -> str:
    # Carries the full identity (so a refresh can re-establish tenant context)
    # plus `jti` == user_sessions.id, the row we rotate/revoke against.
    return _encode(
        {
            "token_use": REFRESH,
            "jti": str(session_id),
            "org": str(claims.organization_id),
            "sub_type": claims.subject_type,
            "sub": str(claims.subject_id),
            "role": claims.role,
        },
        settings.refresh_token_ttl_seconds,
    )


def create_signup_token(organization_id: uuid.UUID, phone: str) -> str:
    """Short-lived proof that `phone` was OTP-verified for this org, so the
    client signup step doesn't re-verify."""
    return _encode(
        {"token_use": SIGNUP, "org": str(organization_id), "phone": phone},
        settings.signup_token_ttl_seconds,
    )


def decode_token(token: str, *, expected_use: str) -> dict:
    """Decode and validate a token. Raises jwt.InvalidTokenError on any problem
    (bad signature, expiry, or wrong token_use)."""
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("token_use") != expected_use:
        raise jwt.InvalidTokenError("unexpected token_use")
    return payload


def access_claims_from_payload(payload: dict) -> AccessClaims:
    return AccessClaims(
        organization_id=uuid.UUID(payload["org"]),
        subject_type=payload["sub_type"],
        subject_id=uuid.UUID(payload["sub"]),
        role=payload.get("role"),
    )
