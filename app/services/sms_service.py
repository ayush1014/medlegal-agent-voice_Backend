"""Outbound SMS via Twilio, sent from the firm's own number and logged to the
DRY comms log (`conversations` + `messages`). Real Twilio at runtime; the network
call (`_twilio_create_message`) is isolated so tests mock it (no real texts)."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import session_scope
from app.security.context import system_context


def _twilio_create_message(from_e164: str, to_e164: str, body: str) -> tuple[str | None, str]:
    """Send one SMS via Twilio. Returns (provider_message_id, status)."""
    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    msg = client.messages.create(to=to_e164, from_=from_e164, body=body)
    return msg.sid, msg.status


async def _resolve_from_number(db: AsyncSession, org: uuid.UUID) -> str | None:
    return (
        await db.execute(
            text("SELECT e164 FROM phone_numbers WHERE organization_id = :o "
                 "ORDER BY is_primary DESC, created_at LIMIT 1"),
            {"o": org},
        )
    ).scalar_one_or_none()


async def _get_or_create_conversation(db: AsyncSession, org: uuid.UUID, lead_id: uuid.UUID) -> uuid.UUID:
    existing = (
        await db.execute(
            text("SELECT id FROM conversations WHERE lead_id = :l AND channel = 'sms' LIMIT 1"),
            {"l": lead_id},
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    cid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO conversations (id, organization_id, lead_id, channel) "
             "VALUES (:id, :o, :l, 'sms')"),
        {"id": cid, "o": org, "l": lead_id},
    )
    return cid


async def send_sms(
    organization_id: uuid.UUID, lead_id: uuid.UUID, to_e164: str, body: str, purpose: str = "follow_up"
) -> str | None:
    """Send an SMS from the firm's number and log it. Returns the provider id (or
    None if the firm has no number / sending failed — always logged)."""
    async with session_scope(system_context(organization_id)) as db:
        from_number = await _resolve_from_number(db, organization_id)
        conversation_id = await _get_or_create_conversation(db, organization_id, lead_id)

    if not from_number:
        return None  # firm has no provisioned number to send from

    try:
        provider_id, status = await asyncio.to_thread(
            _twilio_create_message, from_number, to_e164, body
        )
    except Exception:  # noqa: BLE001 - record the failure, don't crash the flow
        provider_id, status = None, "failed"

    async with session_scope(system_context(organization_id)) as db:
        await db.execute(
            text("INSERT INTO messages (organization_id, conversation_id, lead_id, channel, "
                 "direction, body, purpose, provider_message_id, status, sent_at) "
                 "VALUES (:o,:c,:l,'sms','outbound',:b,:p,:pid,:st, now())"),
            {"o": organization_id, "c": conversation_id, "l": lead_id, "b": body,
             "p": purpose, "pid": provider_id, "st": status},
        )
        await db.execute(
            text("UPDATE conversations SET last_message_at = now() WHERE id = :c"),
            {"c": conversation_id},
        )
    return provider_id


def _portal_link() -> str | None:
    if not settings.frontend_base_url:
        return None
    return settings.frontend_base_url.rstrip("/") + "/client"


async def send_resume_sms(organization_id: uuid.UUID, lead_id: uuid.UUID, to_e164: str) -> str | None:
    link = _portal_link()
    body = (
        "Hi, this is medLegal. It looks like our call ended early. "
        + (f"You can finish your intake here: {link} — or call us back anytime."
           if link else "Please call us back anytime and we'll pick up where we left off.")
    )
    return await send_sms(organization_id, lead_id, to_e164, body, purpose="follow_up")


async def send_callback_sms(organization_id: uuid.UUID, lead_id: uuid.UUID, to_e164: str) -> str | None:
    body = "Thanks for calling medLegal. We received your message and a team member will call you back shortly."
    return await send_sms(organization_id, lead_id, to_e164, body, purpose="follow_up")


async def send_welcome_sms(
    organization_id: uuid.UUID, lead_id: uuid.UUID, to_e164: str, *, email: str | None = None
) -> str | None:
    """After a completed intake — invite the caller to the portal and tell them the
    document/agreement follow-up comes by EMAIL (our doc-intake channel)."""
    link = _portal_link()
    body = "Thanks for calling medLegal — your case has been started. "
    if email:
        body += f"We'll email the documents we need to {email}. "
    body += (f"Track its status here: {link}" if link
             else "We'll text you a secure link to your case shortly.")
    return await send_sms(organization_id, lead_id, to_e164, body, purpose="general")
