"""Short clickable links for SMS/WhatsApp.

Maps a short random code -> (org, lead, purpose) so texted URLs are tiny and
linkify reliably. Owner connection only (creation in the funnel + pre-auth
resolution when the client taps the link); codes are unguessable secrets.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import _build_async_url

UPLOAD = "doc_upload"
SIGN = "retainer_sign"
_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous chars


def _code(n: int = 7) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def _engine():
    return create_async_engine(_build_async_url(settings.database_url), poolclass=NullPool)


async def create(
    organization_id: uuid.UUID, lead_id: uuid.UUID, purpose: str, *, ttl_days: int = 14
) -> str:
    code = _code()
    expires = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    engine = _engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO short_links (code, organization_id, lead_id, purpose, expires_at) "
                     "VALUES (:c,:o,:l,:p,:e)"),
                {"c": code, "o": organization_id, "l": lead_id, "p": purpose, "e": expires},
            )
        return code
    finally:
        await engine.dispose()


async def resolve(code: str) -> dict | None:
    engine = _engine()
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(
                text("SELECT organization_id, lead_id, purpose, expires_at FROM short_links "
                     "WHERE code = :c"), {"c": code},
            )).first()
    finally:
        await engine.dispose()
    if row is None or row.expires_at < datetime.now(timezone.utc):
        return None
    return {"organization_id": row.organization_id, "lead_id": row.lead_id, "purpose": row.purpose}
