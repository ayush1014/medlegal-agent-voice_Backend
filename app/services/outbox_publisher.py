"""Outbox publisher — relays pending domain events to registered handlers.

Connective tissue of the post-intake funnel: emitting `intake.completed` into the
outbox (in the intake transaction) and dispatching it drives scoring,
qualification, settlement, etc.

Two connections, by design:
- Outbox bookkeeping (read pending, advance status) uses the OWNER connection —
  `outbox_events` is append-only for the app role, and the publisher is a trusted
  system process (the model documents this).
- Business handlers run under each org's SYSTEM context (app_user) so their writes
  are RLS-scoped to the tenant.

Per-event isolation: one event's handlers run in their own tenant transaction; on
failure the event is marked `failed` (for retry) without affecting other events.
Handlers should be idempotent — a rare retry may re-run a succeeded handler.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import _build_async_url, session_scope
from app.security.context import system_context

# handler(db, organization_id, aggregate_id, payload) -> awaitable
Handler = Callable[[AsyncSession, uuid.UUID, uuid.UUID | None, dict], Awaitable[None]]

_HANDLERS: dict[str, list[Handler]] = {}


def on(event_type: str) -> Callable[[Handler], Handler]:
    """Register a handler for an event type."""
    def deco(fn: Handler) -> Handler:
        _HANDLERS.setdefault(event_type, []).append(fn)
        return fn
    return deco


def _as_dict(payload) -> dict:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    try:
        return json.loads(payload)
    except (TypeError, ValueError):
        return {}


async def _dispatch(where: str, params: dict, limit: int) -> dict:
    """Process pending outbox events matching `where` (owner conn for bookkeeping)."""
    result = {"published": 0, "failed": 0, "skipped": 0}
    engine = create_async_engine(_build_async_url(settings.database_url), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT id, organization_id, event_type, aggregate_id, payload "
                         "FROM outbox_events WHERE status = 'pending' AND " + where +
                         " ORDER BY available_at LIMIT :n"),
                    {**params, "n": limit},
                )
            ).all()

        for r in rows:
            handlers = _HANDLERS.get(r.event_type, [])
            if not handlers:
                result["skipped"] += 1  # leave pending until a handler exists
                continue
            try:
                async with session_scope(system_context(r.organization_id)) as db:
                    payload = _as_dict(r.payload)
                    for handler in handlers:
                        await handler(db, r.organization_id, r.aggregate_id, payload)
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE outbox_events SET status='published', published_at=now(), "
                             "attempts=attempts+1 WHERE id=:id"),
                        {"id": r.id},
                    )
                result["published"] += 1
            except Exception:  # noqa: BLE001 - isolate failure to this event
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE outbox_events SET status='failed', attempts=attempts+1 "
                             "WHERE id=:id"),
                        {"id": r.id},
                    )
                result["failed"] += 1
        return result
    finally:
        await engine.dispose()


async def dispatch_pending_for_org(organization_id: uuid.UUID, limit: int = 100) -> dict:
    """Process one org's pending events (inline fast path after a call)."""
    return await _dispatch("organization_id = :o", {"o": organization_id}, limit)


async def dispatch_pending(limit: int = 200) -> dict:
    """Process all orgs' pending events (background/cron sweep)."""
    return await _dispatch("TRUE", {}, limit)
