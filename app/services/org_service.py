"""Organization resolution for firm-branded entry points."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_org_id(db: AsyncSession, slug: str) -> uuid.UUID | None:
    """Map a firm slug to its organization id, or None if unknown.

    Uses the SECURITY DEFINER `app.org_id_for_slug` so it works pre-auth (no
    tenant context set) without exposing a global read surface on organizations.
    """
    result = await db.execute(
        text("SELECT app.org_id_for_slug(:slug)"), {"slug": slug}
    )
    return result.scalar_one_or_none()
