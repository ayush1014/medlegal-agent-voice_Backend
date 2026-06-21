"""Signed, time-limited link tokens for client self-service flows reached over
WhatsApp (document upload, retainer signing) — no login required, scoped to one
lead/retainer and one purpose."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings

UPLOAD = "doc_upload"
SIGN = "retainer_sign"


def make_link_token(purpose: str, *, ttl_seconds: int = 7 * 24 * 3600, **claims) -> str:
    payload = {
        "purpose": purpose,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        **{k: str(v) for k, v in claims.items()},
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_link_token(token: str, purpose: str) -> dict:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("purpose") != purpose:
        raise ValueError("wrong token purpose")
    return payload
