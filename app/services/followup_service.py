"""Smart follow-up automation — advance each lead through the funnel and chase stalls over
BOTH email and SMS, dynamically until the goal is met (documents received / LOR signed).

Each tick (cron or the in-app scheduler), per firm:
  Qualified, no docs requested -> request documents      (+ reset the nudge counter)
  All requested docs received  -> send the retainer/LOR  (+ reset the nudge counter)
  Docs still missing           -> reminder (email + SMS), at most once every N hours
  Retainer unsigned            -> reminder (email + SMS), at most once every N hours

Reminders are rate-limited per lead (last_follow_up_at), capped (follow_up_count reaches
followup_max_attempts -> stop + flag a human), and skipped during quiet hours. Idempotent +
per-org isolated; network sends run outside the read transaction.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.config import settings
from app.database import session_scope
from app.security.context import system_context
from app.services import document_service, email_service, messaging_service, retainer_service

logger = logging.getLogger("medlegal.followups")


def _in_quiet_hours(now: datetime) -> bool:
    """True when reminders should pause (outside [start, end) in the firm's timezone)."""
    try:
        hour = now.astimezone(ZoneInfo(settings.firm_timezone)).hour
    except Exception:  # noqa: BLE001 - bad tz string -> fall back to UTC hour
        hour = now.hour
    return not (settings.followup_quiet_start_hour <= hour < settings.followup_quiet_end_hour)


async def _doc_reminder(org: uuid.UUID, lead_id: uuid.UUID, email: str | None, phone: str | None) -> None:
    """Send the 'still waiting on documents' reminder over email + SMS."""
    async with session_scope(system_context(org)) as db:
        checklist = ", ".join(r[0] for r in (await db.execute(
            text("SELECT document_type FROM document_requests WHERE lead_id=:l "
                 "AND status NOT IN ('Received','Waived') ORDER BY created_at"), {"l": lead_id})).all())
    link = await document_service.upload_link(org, lead_id)
    if email and settings.email_enabled:
        body = (
            "Hi, it's medLegal — a quick reminder that we're still waiting on a few documents to "
            f"move your case forward:\n\n{checklist}\n\n"
            "Just reply to this email with the photos or PDFs attached"
            + (f", or upload them securely here: {link}" if link else "")
            + ".\n\nThank you,\nThe medLegal team"
        )
        await email_service.send_email(org, lead_id, email, "Reminder: documents needed for your case",
                                       body, purpose="doc_request")
    if phone:
        sms = ("Quick reminder from medLegal — we're still missing a few documents for your case"
               + (f" ({checklist})" if checklist else "") + ". Reply here with photos whenever you can.")
        await messaging_service.send_message(
            org, lead_id, phone, channel=settings.funnel_channel, purpose="doc_request",
            content_sid=settings.whatsapp_template_nudge, content_vars={"1": "medLegal"}, body=sms)


async def _lor_reminder(org: uuid.UUID, lead_id: uuid.UUID, email: str | None, phone: str | None) -> None:
    """Send the 'agreement ready to sign' reminder over email + SMS (with a fresh sign link)."""
    link = await retainer_service.sign_link(org, lead_id)
    if email and settings.email_enabled:
        body = (
            "Hi, it's medLegal — your representation agreement is ready and just needs your "
            "signature to get started.\n\n"
            + (f"Review and sign it here:\n{link}\n\n" if link else "")
            + "Reply with any questions.\n\nThank you,\nThe medLegal team"
        )
        await email_service.send_email(org, lead_id, email, "Reminder: your agreement is ready to sign",
                                       body, purpose="retainer")
    if phone:
        sms = ("Friendly nudge from medLegal — your representation agreement is ready to sign"
               + (f". Tap to finish: {link}" if link else ". Tap your secure link to finish.")
               + " We'll get right to work once it's signed.")
        await messaging_service.send_message(
            org, lead_id, phone, channel=settings.funnel_channel, purpose="retainer",
            content_sid=settings.whatsapp_template_nudge, content_vars={"1": "medLegal"}, body=sms)


async def _flag_exhausted(org: uuid.UUID, lead_id: uuid.UUID, phase: str) -> None:
    """Record that auto-follow-ups hit the cap, so staff can take over (timeline event)."""
    async with session_scope(system_context(org)) as db:
        await db.execute(
            text("INSERT INTO agent_events (organization_id, lead_id, event_type, name, payload) "
                 "VALUES (:o,:l,'decision','followups_exhausted', CAST(:p AS jsonb))"),
            {"o": org, "l": lead_id, "p": f'{{"phase": "{phase}"}}'})
    logger.info("follow-ups exhausted for lead %s (%s) — flagged for human", lead_id, phase)


async def run_followups(organization_id: uuid.UUID, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=settings.followup_nudge_interval_hours)
    cap = settings.followup_max_attempts
    counts = {"docs_requested": 0, "retainers_sent": 0, "doc_nudges": 0,
              "retainer_nudges": 0, "exhausted": 0}

    # 1. Qualified with no documents requested yet -> request them (reset the nudge counter).
    async with session_scope(system_context(organization_id)) as db:
        ids = [r.id for r in (await db.execute(
            text("SELECT id FROM leads l WHERE l.organization_id=:o AND l.deleted_at IS NULL "
                 "AND l.qualification_status='Qualified' AND l.pipeline_status='Qualified' "
                 "AND NOT EXISTS (SELECT 1 FROM document_requests d WHERE d.lead_id=l.id)"),
            {"o": organization_id})).all()]
    for lid in ids:
        await document_service.request_documents(organization_id, lid)
        async with session_scope(system_context(organization_id)) as db:
            await db.execute(text("UPDATE leads SET pipeline_status='Docs Requested', "
                                  "last_follow_up_at=:n, follow_up_count=0 WHERE id=:l"),
                             {"n": now, "l": lid})
        counts["docs_requested"] += 1

    # 2. All requested docs received -> send the retainer (reset the nudge counter).
    async with session_scope(system_context(organization_id)) as db:
        ids = [r.id for r in (await db.execute(
            text("SELECT id FROM leads WHERE organization_id=:o AND deleted_at IS NULL "
                 "AND pipeline_status IN ('Docs Requested','Docs Received') AND missing_documents=0 "
                 "AND retainer_status='Not Ready' "
                 "AND EXISTS (SELECT 1 FROM document_requests d WHERE d.lead_id=leads.id)"),
            {"o": organization_id})).all()]
    for lid in ids:
        await retainer_service.prepare_and_send(organization_id, lid)
        async with session_scope(system_context(organization_id)) as db:
            await db.execute(text("UPDATE leads SET last_follow_up_at=:n, follow_up_count=0 WHERE id=:l"),
                             {"n": now, "l": lid})
        counts["retainers_sent"] += 1

    # Reminders respect quiet hours; the funnel advances above run any time.
    if _in_quiet_hours(now):
        return counts

    # 3. Docs still missing + due + under the cap -> email + SMS reminder.
    async with session_scope(system_context(organization_id)) as db:
        due = [(r.id, r.email, r.phone, r.follow_up_count) for r in (await db.execute(
            text("SELECT id, email, phone, follow_up_count FROM leads WHERE organization_id=:o "
                 "AND deleted_at IS NULL AND pipeline_status='Docs Requested' AND missing_documents>0 "
                 "AND follow_up_count < :cap "
                 "AND (last_follow_up_at IS NULL OR last_follow_up_at < :cut)"),
            {"o": organization_id, "cap": cap, "cut": cutoff})).all()]
    for lid, email, phone, fc in due:
        await _doc_reminder(organization_id, lid, email, phone)
        async with session_scope(system_context(organization_id)) as db:
            await db.execute(text("UPDATE leads SET last_follow_up_at=:n, "
                                  "follow_up_count=follow_up_count+1 WHERE id=:l"), {"n": now, "l": lid})
        counts["doc_nudges"] += 1
        if fc + 1 >= cap:
            await _flag_exhausted(organization_id, lid, "doc_collection")
            counts["exhausted"] += 1

    # 4. Retainer sent/viewed but unsigned + due + under the cap -> email + SMS reminder.
    async with session_scope(system_context(organization_id)) as db:
        due = [(r.id, r.email, r.phone, r.follow_up_count) for r in (await db.execute(
            text("SELECT id, email, phone, follow_up_count FROM leads WHERE organization_id=:o "
                 "AND deleted_at IS NULL AND pipeline_status='Retainer Sent' "
                 "AND retainer_status IN ('Sent','Viewed') AND follow_up_count < :cap "
                 "AND (last_follow_up_at IS NULL OR last_follow_up_at < :cut)"),
            {"o": organization_id, "cap": cap, "cut": cutoff})).all()]
    for lid, email, phone, fc in due:
        await _lor_reminder(organization_id, lid, email, phone)
        async with session_scope(system_context(organization_id)) as db:
            await db.execute(text("UPDATE leads SET last_follow_up_at=:n, "
                                  "follow_up_count=follow_up_count+1 WHERE id=:l"), {"n": now, "l": lid})
        counts["retainer_nudges"] += 1
        if fc + 1 >= cap:
            await _flag_exhausted(organization_id, lid, "lor_signing")
            counts["exhausted"] += 1

    return counts
