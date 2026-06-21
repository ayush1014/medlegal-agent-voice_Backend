"""Channel-aware outbound + inbound messaging (Twilio), logged to the comms log.

The funnel uses WhatsApp (follow-ups + document gathering); SMS stays available.
Outbound and inbound both persist to `conversations` + `messages` so the whole
client thread is one auditable log. The Twilio network call (`_twilio_send`) is
isolated so tests mock it.

WhatsApp note: business-initiated messages outside the 24h customer window must be
approved templates — pass `content_sid` (+ `content_vars`) for those; freeform
`body` works in-session and in the Twilio sandbox.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import _build_async_url, session_scope
from app.security.context import system_context


def _twilio_send(
    *, from_addr: str, to_addr: str, body: str | None,
    media_url: str | None = None, content_sid: str | None = None,
    content_vars: dict | None = None,
) -> tuple[str | None, str]:
    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    kwargs: dict = {"to": to_addr, "from_": from_addr}
    if content_sid:
        kwargs["content_sid"] = content_sid
        if content_vars:
            kwargs["content_variables"] = json.dumps(content_vars)
    else:
        kwargs["body"] = body or ""
    if media_url:
        kwargs["media_url"] = [media_url]
    msg = client.messages.create(**kwargs)
    return msg.sid, msg.status


def _addr(channel: str, e164: str) -> str:
    return f"whatsapp:{e164}" if channel == "whatsapp" else e164


async def _from_number(db: AsyncSession, org: uuid.UUID, channel: str) -> str | None:
    if channel == "whatsapp":
        return settings.twilio_whatsapp_number
    return (
        await db.execute(
            text("SELECT e164 FROM phone_numbers WHERE organization_id = :o "
                 "ORDER BY is_primary DESC, created_at LIMIT 1"),
            {"o": org},
        )
    ).scalar_one_or_none()


async def get_or_create_conversation(
    db: AsyncSession, org: uuid.UUID, lead_id: uuid.UUID, channel: str
) -> uuid.UUID:
    existing = (
        await db.execute(
            text("SELECT id FROM conversations WHERE lead_id = :l AND channel = :c LIMIT 1"),
            {"l": lead_id, "c": channel},
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    cid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO conversations (id, organization_id, lead_id, channel) "
             "VALUES (:id, :o, :l, :c)"),
        {"id": cid, "o": org, "l": lead_id, "c": channel},
    )
    return cid


async def send_message(
    organization_id: uuid.UUID,
    lead_id: uuid.UUID,
    to_e164: str,
    *,
    body: str | None = None,
    channel: str = "whatsapp",
    purpose: str = "follow_up",
    media_url: str | None = None,
    content_sid: str | None = None,
    content_vars: dict | None = None,
) -> str | None:
    """Send a message on `channel` from the firm and log it. Returns the provider
    id (or None if no sender configured / send failed — always logged)."""
    async with session_scope(system_context(organization_id)) as db:
        from_number = await _from_number(db, organization_id, channel)
        conversation_id = await get_or_create_conversation(db, organization_id, lead_id, channel)

    if not from_number:
        return None

    # Content templates are WhatsApp-only; SMS/other channels send the freeform body.
    if channel != "whatsapp":
        content_sid, content_vars = None, None

    try:
        provider_id, status = await asyncio.to_thread(
            _twilio_send,
            from_addr=_addr(channel, from_number), to_addr=_addr(channel, to_e164),
            body=body, media_url=media_url, content_sid=content_sid, content_vars=content_vars,
        )
    except Exception:  # noqa: BLE001 - record failure, never break the funnel
        provider_id, status = None, "failed"

    media = {"url": media_url} if media_url else None
    async with session_scope(system_context(organization_id)) as db:
        await db.execute(
            text("INSERT INTO messages (organization_id, conversation_id, lead_id, channel, "
                 "direction, body, media, purpose, provider_message_id, status, sent_at) "
                 "VALUES (:o,:c,:l,:ch,'outbound',:b, CAST(:media AS jsonb),:p,:pid,:st, now())"),
            {"o": organization_id, "c": conversation_id, "l": lead_id, "ch": channel,
             "b": body, "media": json.dumps(media) if media else None,
             "p": purpose, "pid": provider_id, "st": status},
        )
        await db.execute(
            text("UPDATE conversations SET last_message_at = now() WHERE id = :c"),
            {"c": conversation_id},
        )
    return provider_id


async def resolve_lead_by_phone(phone: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Find (org, lead) for an inbound message by the sender's phone. Cross-org
    lookup uses the owner connection (a single WhatsApp sender serves all firms)."""
    engine = create_async_engine(_build_async_url(settings.database_url), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(
                text("SELECT organization_id, id FROM leads WHERE phone = :p "
                     "AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1"),
                {"p": phone},
            )).first()
        return (row.organization_id, row.id) if row else None
    finally:
        await engine.dispose()


async def record_inbound(
    organization_id: uuid.UUID,
    lead_id: uuid.UUID,
    *,
    channel: str = "whatsapp",
    body: str | None = None,
    media: list[dict] | None = None,
    provider_message_id: str | None = None,
) -> uuid.UUID:
    """Persist an inbound client message (e.g. WhatsApp reply / media)."""
    async with session_scope(system_context(organization_id)) as db:
        conversation_id = await get_or_create_conversation(db, organization_id, lead_id, channel)
        await db.execute(
            text("INSERT INTO messages (organization_id, conversation_id, lead_id, channel, "
                 "direction, body, media, purpose, provider_message_id, status) "
                 "VALUES (:o,:c,:l,:ch,'inbound',:b, CAST(:media AS jsonb),'general',:pid,'received') "
                 "ON CONFLICT (provider_message_id) DO NOTHING"),
            {"o": organization_id, "c": conversation_id, "l": lead_id, "ch": channel,
             "b": body, "media": json.dumps({"items": media}) if media else None,
             "pid": provider_message_id},
        )
        await db.execute(
            text("UPDATE conversations SET last_message_at = now() WHERE id = :c"),
            {"c": conversation_id},
        )
    return conversation_id
