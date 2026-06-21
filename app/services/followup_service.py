"""Smart follow-up automation — advances each lead through the funnel over WhatsApp.

A tick (cron or manual) moves leads forward and nudges stalls:
  Qualified            -> request documents (Docs Requested)
  Docs all received    -> send the retainer/LOR (Retainer Sent)
  Docs still missing   -> reminder nudge (rate-limited by last_follow_up_at)
  Retainer unsigned    -> reminder nudge

Network actions (WhatsApp) run via the services' own sessions, never inside the
read transaction. Idempotent + rate-limited so repeated ticks don't spam.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.config import settings
from app.database import session_scope
from app.security.context import system_context
from app.services import document_service, messaging_service, retainer_service


async def run_followups(
    organization_id: uuid.UUID, *, now: datetime | None = None, stale_days: int = 2
) -> dict:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=stale_days)
    counts = {"docs_requested": 0, "retainers_sent": 0, "doc_nudges": 0, "retainer_nudges": 0}

    # 1. Qualified with no documents requested yet -> request them.
    async with session_scope(system_context(organization_id)) as db:
        ids = [r.id for r in (await db.execute(
            text("SELECT id FROM leads l WHERE l.organization_id=:o AND l.deleted_at IS NULL "
                 "AND l.qualification_status='Qualified' AND l.pipeline_status='Qualified' "
                 "AND NOT EXISTS (SELECT 1 FROM document_requests d WHERE d.lead_id=l.id)"),
            {"o": organization_id})).all()]
    for lid in ids:
        await document_service.request_documents(organization_id, lid)
        async with session_scope(system_context(organization_id)) as db:
            await db.execute(
                text("UPDATE leads SET pipeline_status='Docs Requested', last_follow_up_at=:n WHERE id=:l"),
                {"n": now, "l": lid})
        counts["docs_requested"] += 1

    # 2. All requested docs received -> send the retainer.
    async with session_scope(system_context(organization_id)) as db:
        ids = [r.id for r in (await db.execute(
            text("SELECT id FROM leads WHERE organization_id=:o AND deleted_at IS NULL "
                 "AND pipeline_status IN ('Docs Requested','Docs Received') AND missing_documents=0 "
                 "AND retainer_status='Not Ready' "
                 "AND EXISTS (SELECT 1 FROM document_requests d WHERE d.lead_id=leads.id)"),
            {"o": organization_id})).all()]
    for lid in ids:
        await retainer_service.prepare_and_send(organization_id, lid)
        counts["retainers_sent"] += 1

    # 3. Docs still missing + stale -> nudge.
    async with session_scope(system_context(organization_id)) as db:
        rows = (await db.execute(
            text("SELECT id, phone FROM leads WHERE organization_id=:o AND deleted_at IS NULL "
                 "AND pipeline_status='Docs Requested' AND missing_documents>0 "
                 "AND (last_follow_up_at IS NULL OR last_follow_up_at < :cut)"),
            {"o": organization_id, "cut": cutoff})).all()
        nudges = [(r.id, r.phone) for r in rows]
    for lid, phone in nudges:
        if phone:
            await messaging_service.send_message(
                organization_id, lid, phone, channel=settings.funnel_channel, purpose="doc_request",
                content_sid=settings.whatsapp_template_nudge, content_vars={"1": "medLegal"},
                body="Quick reminder from medLegal — we're still missing a few documents for your case. "
                     "Reply here with photos or use your secure upload link whenever you can.")
        async with session_scope(system_context(organization_id)) as db:
            await db.execute(text("UPDATE leads SET last_follow_up_at=:n WHERE id=:l"), {"n": now, "l": lid})
        counts["doc_nudges"] += 1

    # 4. Retainer sent/viewed but unsigned + stale -> nudge.
    async with session_scope(system_context(organization_id)) as db:
        rows = (await db.execute(
            text("SELECT id, phone FROM leads WHERE organization_id=:o AND deleted_at IS NULL "
                 "AND pipeline_status='Retainer Sent' AND retainer_status IN ('Sent','Viewed') "
                 "AND (last_follow_up_at IS NULL OR last_follow_up_at < :cut)"),
            {"o": organization_id, "cut": cutoff})).all()
        nudges = [(r.id, r.phone) for r in rows]
    for lid, phone in nudges:
        if phone:
            await messaging_service.send_message(
                organization_id, lid, phone, channel=settings.funnel_channel, purpose="retainer",
                content_sid=settings.whatsapp_template_nudge, content_vars={"1": "medLegal"},
                body="Just a friendly nudge from medLegal — your representation agreement is ready to sign. "
                     "Tap your secure link to finish, and we'll get right to work on your case.")
        async with session_scope(system_context(organization_id)) as db:
            await db.execute(text("UPDATE leads SET last_follow_up_at=:n WHERE id=:l"), {"n": now, "l": lid})
        counts["retainer_nudges"] += 1

    return counts
