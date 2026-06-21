"""Persistence for a voice intake: partial lead, transcript, segments, agent
state, and agent events. All calls run under the firm's system context.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import IntakeContext

# Lead columns the agent may update mid-call (headline only; structured facts are
# extracted post-call into child tables).
_PARTIAL_LEAD_FIELDS = {
    "full_name",
    "email",
    "case_type",
    "preferred_contact_method",
    "best_time_to_contact",
    "ai_summary",
}


async def create_session_records(db: AsyncSession, ctx: IntakeContext) -> None:
    """Resolve-or-create the lead by phone (so a returning caller keeps ONE profile),
    then create this call's transcript + agent thread; populate ctx ids."""
    existing = None
    if ctx.caller_phone and ctx.caller_phone not in ("", "unknown"):
        existing = (
            await db.execute(
                text("SELECT id, full_name FROM leads WHERE organization_id = :org "
                     "AND phone = :phone AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1"),
                {"org": ctx.organization_id, "phone": ctx.caller_phone},
            )
        ).first()

    if existing is not None:
        lead_id = existing.id
        ctx.returning = True
        ctx.known_name = existing.full_name if existing.full_name != "Caller" else None
    else:
        lead_id = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO leads (id, organization_id, full_name, phone, case_type, "
                "source, pipeline_status) "
                "VALUES (:id, :org, 'Caller', :phone, 'Other Personal Injury', "
                "'inbound_call', 'Intake Started')"
            ),
            {"id": lead_id, "org": ctx.organization_id, "phone": ctx.caller_phone},
        )

    transcript_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO intake_transcripts (id, organization_id, lead_id, voice_call_id, "
            "language, status) VALUES (:id, :org, :lead, :call, :lang, 'in_progress')"
        ),
        {"id": transcript_id, "org": ctx.organization_id, "lead": lead_id,
         "call": ctx.voice_call_id, "lang": ctx.language},
    )

    thread_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO agent_threads (id, organization_id, lead_id, thread_key, status, "
            "last_active_at) VALUES (:id, :org, :lead, :key, 'active', now())"
        ),
        # Unique per call (thread_id is fresh each call) — a returning caller reusing
        # the same lead must not collide on thread_key.
        {"id": thread_id, "org": ctx.organization_id, "lead": lead_id,
         "key": f"call:{ctx.voice_call_id or thread_id}"},
    )

    ctx.lead_id = lead_id
    ctx.transcript_id = transcript_id
    ctx.agent_thread_id = thread_id


async def add_segment(db: AsyncSession, ctx: IntakeContext, speaker: str, content: str) -> None:
    await db.execute(
        text(
            "INSERT INTO transcript_segments (organization_id, transcript_id, lead_id, seq, "
            "speaker, text) VALUES (:org, :tid, :lead, :seq, :speaker, :text)"
        ),
        {"org": ctx.organization_id, "tid": ctx.transcript_id, "lead": ctx.lead_id,
         "seq": ctx.next_seq(), "speaker": speaker, "text": content},
    )


async def log_event(
    db: AsyncSession, ctx: IntakeContext, *, event_type: str, name: str, payload: dict | None = None
) -> None:
    await db.execute(
        text(
            "INSERT INTO agent_events (organization_id, lead_id, agent_thread_id, event_type, "
            "name, payload) VALUES (:org, :lead, :thread, :etype, :name, CAST(:payload AS jsonb))"
        ),
        {"org": ctx.organization_id, "lead": ctx.lead_id, "thread": ctx.agent_thread_id,
         "etype": event_type, "name": name, "payload": json.dumps(payload or {})},
    )


async def update_partial_lead(db: AsyncSession, ctx: IntakeContext, fields: dict) -> None:
    updates = {k: v for k, v in fields.items() if k in _PARTIAL_LEAD_FIELDS and v is not None}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    await db.execute(
        text(f"UPDATE leads SET {set_clause} WHERE id = :lead_id"),
        {**updates, "lead_id": ctx.lead_id},
    )


async def build_dossier(db: AsyncSession, lead_id: uuid.UUID) -> str | None:
    """A concise 'what we already know' brief for a returning caller, assembled
    from the lead + child tables. None if there's nothing meaningful yet."""
    lead = (await db.execute(
        text("SELECT full_name, case_type, pipeline_status, ai_summary FROM leads WHERE id=:l"),
        {"l": lead_id})).first()
    if lead is None:
        return None
    injuries = [r[0] for r in (await db.execute(
        text("SELECT body_part FROM injuries WHERE lead_id=:l AND body_part IS NOT NULL"),
        {"l": lead_id})).all()]
    treatments = [r[0] for r in (await db.execute(
        text("SELECT DISTINCT provider_name FROM medical_treatments WHERE lead_id=:l "
             "AND provider_name IS NOT NULL"), {"l": lead_id})).all()]
    inc = (await db.execute(
        text("SELECT incident_date, description FROM incidents WHERE lead_id=:l "
             "ORDER BY created_at LIMIT 1"), {"l": lead_id})).first()

    named = lead.full_name and lead.full_name != "Caller"
    has_case = lead.case_type and lead.case_type != "Other Personal Injury"
    if not (named or has_case or injuries or treatments or inc or lead.ai_summary):
        return None

    parts: list[str] = []
    if named:
        parts.append(f"Name: {lead.full_name}.")
    if has_case:
        parts.append(f"Case type: {lead.case_type}.")
    if inc and inc.description:
        parts.append(f"Incident: {inc.description}.")
    if injuries:
        parts.append("Injuries: " + ", ".join(sorted(set(injuries))) + ".")
    if treatments:
        parts.append("Treatment with: " + ", ".join(treatments) + ".")
    if lead.pipeline_status:
        parts.append(f"Current stage: {lead.pipeline_status}.")
    if lead.ai_summary:
        parts.append(f"Prior summary: {lead.ai_summary}")
    return " ".join(parts)


async def lookup_prior_lead(db: AsyncSession, ctx: IntakeContext) -> dict | None:
    """Most recent prior lead for this caller's number (for returning-caller resume)."""
    row = (
        await db.execute(
            text(
                "SELECT id, full_name, case_type, ai_summary FROM leads "
                "WHERE organization_id = :org AND phone = :phone AND deleted_at IS NULL "
                "AND id <> :current ORDER BY created_at DESC LIMIT 1"
            ),
            {"org": ctx.organization_id, "phone": ctx.caller_phone,
             "current": ctx.lead_id or uuid.uuid4()},
        )
    ).first()
    if row is None:
        return None
    return {"full_name": row.full_name, "case_type": row.case_type, "summary": row.ai_summary}


async def set_language(db: AsyncSession, ctx: IntakeContext) -> None:
    await db.execute(
        text("UPDATE intake_transcripts SET language = :lang WHERE id = :tid"),
        {"lang": ctx.language, "tid": ctx.transcript_id},
    )


async def finalize_transcript(
    db: AsyncSession, ctx: IntakeContext, *, status: str, full_text: str
) -> None:
    await db.execute(
        text(
            "UPDATE intake_transcripts SET status = :status, full_text = :full WHERE id = :tid"
        ),
        {"status": status, "full": full_text, "tid": ctx.transcript_id},
    )
    await db.execute(
        text("UPDATE agent_threads SET status = 'complete', last_active_at = now() WHERE id = :id"),
        {"id": ctx.agent_thread_id},
    )
