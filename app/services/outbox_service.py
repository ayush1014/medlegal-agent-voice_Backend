"""Transactional outbox: write a domain event in the same tx as the change that
caused it. A publisher (later) relays pending rows to consumers (PRD-3 scoring,
follow-ups). Emit must be called inside the caller's DB transaction."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def emit_event(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID | None,
    event_type: str,
    payload: dict,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO outbox_events (id, organization_id, aggregate_type, aggregate_id, "
            "event_type, payload) VALUES (:id, :o, :at, :aid, :et, CAST(:p AS jsonb))"
        ),
        {"id": event_id, "o": organization_id, "at": aggregate_type, "aid": aggregate_id,
         "et": event_type, "p": json.dumps(payload)},
    )
    return event_id
