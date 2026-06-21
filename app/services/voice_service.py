"""Telephony ingress helpers: org resolution, voice-call records, webhook dedupe.

Org resolution runs pre-context (SECURITY DEFINER resolver); call writes run under
the firm's system context. Webhook idempotency uses the infra `webhook_events`
table (no tenant scope).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_org_by_dialed_number(db: AsyncSession, e164: str) -> uuid.UUID | None:
    """Map a dialed Twilio number to its organization (or None if unknown)."""
    result = await db.execute(
        text("SELECT app.org_id_for_phone(:e164)"), {"e164": e164}
    )
    return result.scalar_one_or_none()


async def claim_webhook(db: AsyncSession, provider: str, event_id: str, event_type: str) -> bool:
    """Record a provider webhook once. Returns True if this is the first time we've
    seen `event_id` (i.e. the caller should process it), False on a retry."""
    result = await db.execute(
        text(
            "INSERT INTO webhook_events (provider, provider_event_id, event_type, status) "
            "VALUES (:p, :eid, :etype, 'received') "
            "ON CONFLICT (provider_event_id) DO NOTHING RETURNING id"
        ),
        {"p": provider, "eid": event_id, "etype": event_type},
    )
    return result.first() is not None


async def get_voice_call_by_sid(db: AsyncSession, provider_sid: str) -> uuid.UUID | None:
    result = await db.execute(
        text("SELECT id FROM voice_calls WHERE provider_sid = :sid"),
        {"sid": provider_sid},
    )
    return result.scalar_one_or_none()


async def create_voice_call(
    db: AsyncSession,
    org: uuid.UUID,
    *,
    direction: str,
    from_e164: str | None,
    to_e164: str | None,
    provider_sid: str,
    status: str | None,
) -> uuid.UUID:
    call_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO voice_calls (id, organization_id, direction, from_e164, to_e164, "
            "provider_sid, status, started_at) "
            "VALUES (:id, :org, :dir, :frm, :to, :sid, :status, now())"
        ),
        {
            "id": call_id,
            "org": org,
            "dir": direction,
            "frm": from_e164,
            "to": to_e164,
            "sid": provider_sid,
            "status": status,
        },
    )
    return call_id


async def create_fallback_lead(
    db: AsyncSession, org: uuid.UUID, caller_phone: str | None, reason: str
) -> uuid.UUID:
    """A missed/voicemail/degraded call must never drop a lead. Create a partial
    lead flagged for human follow-up."""
    lead_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO leads (id, organization_id, full_name, phone, case_type, source, "
            "pipeline_status, ai_summary) VALUES (:id, :o, 'Caller', :phone, "
            "'Other Personal Injury', 'inbound_call', 'Needs Review', :reason)"
        ),
        {"id": lead_id, "o": org, "phone": caller_phone or "unknown", "reason": reason},
    )
    return lead_id


async def link_voice_call_lead(db: AsyncSession, voice_call_id: uuid.UUID, lead_id: uuid.UUID) -> None:
    await db.execute(
        text("UPDATE voice_calls SET lead_id = :lead WHERE id = :id"),
        {"lead": lead_id, "id": voice_call_id},
    )


async def get_voice_call_summary(db: AsyncSession, provider_sid: str):
    return (
        await db.execute(
            text("SELECT id, from_e164, lead_id FROM voice_calls WHERE provider_sid = :sid"),
            {"sid": provider_sid},
        )
    ).first()


async def find_in_progress_lead(db: AsyncSession, voice_call_id: uuid.UUID) -> uuid.UUID | None:
    """A still-in-progress transcript for an ended call means the call dropped."""
    return (
        await db.execute(
            text("SELECT lead_id FROM intake_transcripts WHERE voice_call_id = :v "
                 "AND status = 'in_progress' ORDER BY created_at DESC LIMIT 1"),
            {"v": voice_call_id},
        )
    ).scalar_one_or_none()


async def finalize_voice_call(
    db: AsyncSession,
    provider_sid: str,
    *,
    status: str | None,
    duration_seconds: int | None,
    recording_url: str | None = None,
) -> None:
    await db.execute(
        text(
            "UPDATE voice_calls SET status = COALESCE(:status, status), "
            "duration_seconds = COALESCE(:dur, duration_seconds), "
            "recording_url = COALESCE(:rec, recording_url), "
            "ended_at = COALESCE(ended_at, now()) "
            "WHERE provider_sid = :sid"
        ),
        {"status": status, "dur": duration_seconds, "rec": recording_url, "sid": provider_sid},
    )
