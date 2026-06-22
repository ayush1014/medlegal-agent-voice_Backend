"""Post-call processor — drains `call.ended` outbox events and runs the heavy
post-call pipeline (extraction → memory → intelligence → welcome SMS) OUTSIDE the
voice worker's shutdown path, so a hangup can never kill it mid-flight.

The voice worker emits `call.ended` in the same fast transaction that finalizes the
transcript. This processor (run by the API process on an interval — see main.py)
picks those up, loads the transcript, and runs the pipeline. Network (LLM,
embeddings, SMS) happens inside run_post_call_pipeline, OFF any held DB tx.

Outbox bookkeeping uses the OWNER connection (append-only for the app role); the
pipeline's own writes run under each org's system context. Failures retry with
backoff up to MAX_ATTEMPTS, then park as `failed` for inspection.
"""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import NEON_CONNECT_ARGS, _build_async_url, session_scope
from app.security.context import system_context
from app.services.intake_pipeline import run_post_call_pipeline

logger = logging.getLogger("medlegal.postcall")

MAX_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = 30


def _as_dict(payload) -> dict:
    if isinstance(payload, dict):
        return payload
    try:
        return json.loads(payload) if payload else {}
    except (TypeError, ValueError):
        return {}


def _uuid(v) -> uuid.UUID | None:
    return uuid.UUID(v) if v else None


async def _load_call_context(org: uuid.UUID, transcript_id: uuid.UUID | None,
                             voice_call_id: uuid.UUID | None) -> tuple[str | None, int | None]:
    """Read the saved transcript text (+ call duration) under the org's context."""
    async with session_scope(system_context(org)) as db:
        transcript_text = None
        if transcript_id is not None:
            row = (await db.execute(
                text("SELECT full_text FROM intake_transcripts WHERE id = :t"), {"t": transcript_id})).first()
            transcript_text = row.full_text if row else None
        duration = None
        if voice_call_id is not None:
            row = (await db.execute(
                text("SELECT duration_seconds FROM voice_calls WHERE id = :v"), {"v": voice_call_id})).first()
            duration = row.duration_seconds if row else None
    return transcript_text, duration


async def process_pending_call_ended(limit: int = 20) -> dict:
    """Drain pending `call.ended` events. Returns {processed, failed, retried}."""
    result = {"processed": 0, "failed": 0, "retried": 0}
    engine = create_async_engine(_build_async_url(settings.database_url), poolclass=NullPool, connect_args=NEON_CONNECT_ARGS)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(
                text("SELECT id, organization_id, aggregate_id, payload, attempts FROM outbox_events "
                     "WHERE status = 'pending' AND event_type = 'call.ended' AND available_at <= now() "
                     "ORDER BY available_at LIMIT :n"),
                {"n": limit})).all()

        for r in rows:
            try:
                p = _as_dict(r.payload)
                transcript_id = _uuid(p.get("transcript_id"))
                voice_call_id = _uuid(p.get("voice_call_id"))
                transcript_text, duration = await _load_call_context(
                    r.organization_id, transcript_id, voice_call_id)
                if not transcript_text:
                    raise RuntimeError(f"no transcript text for call.ended (transcript {transcript_id})")

                await run_post_call_pipeline(
                    organization_id=r.organization_id,
                    lead_id=r.aggregate_id,
                    transcript_text=transcript_text,
                    transcript_id=transcript_id,
                    voice_call_id=voice_call_id,
                    caller_phone=p.get("caller_phone"),
                    duration_seconds=duration,
                )
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE outbox_events SET status='published', published_at=now(), "
                             "attempts=attempts+1 WHERE id=:id"), {"id": r.id})
                result["processed"] += 1
                logger.info("post-call processed lead=%s", r.aggregate_id)
            except Exception:  # noqa: BLE001 - isolate failure to this event
                give_up = (r.attempts + 1) >= MAX_ATTEMPTS
                logger.exception("post-call failed for event %s lead %s (attempt %d%s)",
                                 r.id, r.aggregate_id, r.attempts + 1, ", giving up" if give_up else ", will retry")
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE outbox_events SET status = :st, attempts = attempts + 1, "
                             "available_at = now() + make_interval(secs => :backoff) WHERE id = :id"),
                        {"st": "failed" if give_up else "pending",
                         "backoff": RETRY_BACKOFF_SECONDS, "id": r.id})
                result["failed" if give_up else "retried"] += 1
        return result
    finally:
        await engine.dispose()
