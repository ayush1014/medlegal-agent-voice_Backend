"""Email via Gmail SMTP — the MVP document-intake + retainer channel.

Outbound only here (sending). smtplib is blocking, so each send runs in a worker
thread off the event loop. Every send is also recorded as a `messages` row
(channel='email') so it shows on the lead timeline, mirroring SMS/WhatsApp.

Inbound (clients replying with attachments) lives in app/jobs/email_inbound.py.
A domain + Resend can replace this later with zero call-site changes.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import uuid
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import session_scope
from app.security.context import system_context

logger = logging.getLogger("medlegal.email")


def _send_sync(to: str, subject: str, body: str, reply_to: str | None) -> str:
    """Blocking SMTP send (runs in a thread). Returns the Message-ID."""
    msg = EmailMessage()
    msg["From"] = formataddr((settings.gmail_from_name, settings.gmail_user))
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    message_id = make_msgid()
    msg["Message-ID"] = message_id
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        server.starttls()
        server.login(settings.gmail_user, settings.gmail_app_password)
        server.send_message(msg)
    return message_id


async def _record(
    org: uuid.UUID, lead_id: uuid.UUID, *, direction: str, body: str, purpose: str,
    provider_id: str, status: str = "sent",
) -> None:
    """Log the email on the lead's timeline (email conversation + message row)."""
    async with session_scope(system_context(org)) as db:
        cid = (await db.execute(
            text("SELECT id FROM conversations WHERE lead_id=:l AND channel='email' LIMIT 1"),
            {"l": lead_id})).scalar_one_or_none()
        if cid is None:
            cid = uuid.uuid4()
            await db.execute(
                text("INSERT INTO conversations (id, organization_id, lead_id, channel) "
                     "VALUES (:i,:o,:l,'email')"), {"i": cid, "o": org, "l": lead_id})
        await db.execute(
            text("INSERT INTO messages (organization_id, conversation_id, lead_id, channel, direction, "
                 "body, purpose, provider_message_id, status, sent_at) "
                 "VALUES (:o,:c,:l,'email',:d,:b,:p,:pid,:st, now())"),
            {"o": org, "c": cid, "l": lead_id, "d": direction, "b": body[:4000], "p": purpose,
             "pid": provider_id, "st": status})
        await db.execute(text("UPDATE conversations SET last_message_at=now() WHERE id=:c"), {"c": cid})


async def send_email(
    organization_id: uuid.UUID, lead_id: uuid.UUID, to: str | None, subject: str, body: str,
    *, reply_to: str | None = None, purpose: str = "general",
) -> str | None:
    """Send an email via Gmail SMTP + record it on the timeline. Returns Message-ID
    (or None if email isn't configured / no recipient)."""
    if not settings.email_enabled:
        logger.warning("email not configured — skipping send to %s", to)
        return None
    if not to:
        return None
    message_id = await asyncio.to_thread(_send_sync, to, subject, body, reply_to)
    try:
        await _record(organization_id, lead_id, direction="outbound", body=body,
                      purpose=purpose, provider_id=message_id)
    except Exception:  # noqa: BLE001 - send already succeeded; timeline log is best-effort
        logger.exception("email sent but timeline record failed (lead %s)", lead_id)
    return message_id


async def record_inbound(
    organization_id: uuid.UUID, lead_id: uuid.UUID, *, body: str, provider_id: str,
) -> None:
    """Log a received client email (with its attachments handled separately)."""
    await _record(organization_id, lead_id, direction="inbound", body=body,
                  purpose="doc_request", provider_id=provider_id, status="received")
