"""Returning-caller identity verification.

When someone calls from a number we've seen, the agent must verify identity
(name + DOB) before sharing any prior case context. The match logic lives here so
it's unit-tested; the voice worker calls it through a tool.
"""

from __future__ import annotations

import re
import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _tokens(name: str | None) -> list[str]:
    if not name:
        return []
    return [t for t in re.sub(r"[^a-z0-9 ]", " ", name.lower()).split() if t]


def name_matches(stored: str | None, provided: str | None) -> bool:
    """Caller must say a name whose first AND last token match what we stored
    (order-insensitive, case/punctuation-insensitive)."""
    s, p = _tokens(stored), set(_tokens(provided))
    if not s or not p:
        return False
    return s[0] in p and s[-1] in p


def dob_matches(stored: date | None, provided: date | None) -> bool:
    return stored is not None and provided is not None and stored == provided


def verify(prior: dict, provided_name: str | None, provided_dob: date | None) -> bool:
    """Both name and DOB must match the stored prior lead."""
    return name_matches(prior.get("full_name"), provided_name) and dob_matches(
        prior.get("date_of_birth"), provided_dob
    )


async def find_prior_caller(
    db: AsyncSession,
    organization_id: uuid.UUID,
    phone: str,
    *,
    exclude_lead_id: uuid.UUID | None = None,
) -> dict | None:
    """Most recent real prior lead for this caller's number (a known identity we
    can verify against). Ignores placeholder 'Caller' rows."""
    sql = (
        "SELECT id, full_name, date_of_birth, case_type, pipeline_status, ai_summary "
        "FROM leads WHERE organization_id = :o AND phone = :p AND deleted_at IS NULL "
        "AND full_name <> 'Caller' "
    )
    params: dict = {"o": organization_id, "p": phone}
    if exclude_lead_id is not None:
        sql += "AND id <> :ex "
        params["ex"] = exclude_lead_id
    sql += "ORDER BY created_at DESC LIMIT 1"
    row = (await db.execute(text(sql), params)).first()
    if row is None:
        return None
    return {
        "lead_id": row.id,
        "full_name": row.full_name,
        "date_of_birth": row.date_of_birth,
        "case_type": row.case_type,
        "pipeline_status": row.pipeline_status,
        "summary": row.ai_summary,
        # Only set once we have a DOB on file to actually verify against.
        "verifiable": row.date_of_birth is not None,
    }
