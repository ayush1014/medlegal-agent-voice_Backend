"""Retainer / Letter of Representation (LOR) closing.

Generates the LOR, stores it, and sends the client a WhatsApp link to an internal
(mock) e-sign page. Signing records the append-only signature_events trail and
advances the lead to Signed. Clean seam to swap in DocuSign/Dropbox Sign later.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import session_scope
from app.security.context import system_context
from app.config import settings
from app.services import document_service, email_service, messaging_service, short_links

_ESIGN_PROVIDER = "internal_mock"


def _lor_text(firm: str, client_name: str, case_type: str) -> str:
    return (
        f"LETTER OF REPRESENTATION\n\n"
        f"This confirms that {firm} agrees to represent {client_name} in connection with a "
        f"{case_type} matter, on a contingency-fee basis as permitted by law.\n\n"
        f"By signing below, {client_name} retains {firm} to pursue this claim and authorizes the "
        f"firm to communicate with insurers, providers, and other parties on the client's behalf.\n\n"
        f"Client signature: ______________________    Date: __________\n"
    )


async def sign_link(org: uuid.UUID, lead_id: uuid.UUID) -> str:
    """Short, clickable sign URL served by the backend. Empty if no public base."""
    base = (settings.public_base_url or "").rstrip("/")
    if not base:
        return ""
    code = await short_links.create(org, lead_id, short_links.SIGN)
    return f"{base}/u/{code}"


async def prepare_and_send(
    organization_id: uuid.UUID, lead_id: uuid.UUID, to_e164: str | None = None
) -> dict:
    async with session_scope(system_context(organization_id)) as db:
        lead = (await db.execute(
            text("SELECT full_name, phone, case_type, email FROM leads WHERE id=:l"),
            {"l": lead_id})).first()
        firm = (await db.execute(
            text("SELECT name FROM organizations WHERE id=:o"), {"o": organization_id})).scalar_one_or_none()
        if lead is None:
            raise ValueError("lead not found")
        to_e164 = to_e164 or lead.phone

        lor = _lor_text(firm or "medLegal", lead.full_name, lead.case_type)
        path = f"{organization_id}/{lead_id}/retainer_{uuid.uuid4().hex}.txt"
        doc_url = document_service._store_object(path, lor.encode("utf-8"), "text/plain")

        retainer_id = uuid.uuid4()
        row = (await db.execute(
            text("INSERT INTO retainers (id, organization_id, lead_id, status, template_id, "
                 "document_url, esign_provider, sent_at) "
                 "VALUES (:id,:o,:l,'Sent','lor-v1',:url,:prov, now()) "
                 "ON CONFLICT (lead_id) DO UPDATE SET status='Sent', document_url=:url, "
                 "esign_provider=:prov, sent_at=now() RETURNING id"),
            {"id": retainer_id, "o": organization_id, "l": lead_id, "url": doc_url, "prov": _ESIGN_PROVIDER},
        )).scalar_one()
        await db.execute(
            text("INSERT INTO signature_events (organization_id, retainer_id, lead_id, event, actor) "
                 "VALUES (:o,:r,:l,'sent','system')"),
            {"o": organization_id, "r": row, "l": lead_id},
        )
        await db.execute(
            text("UPDATE leads SET retainer_status='Sent', pipeline_status='Retainer Sent' WHERE id=:l"),
            {"l": lead_id},
        )

    link = await sign_link(organization_id, lead_id)
    channel = "email" if (lead.email and settings.email_enabled) else settings.funnel_channel
    if lead.email and settings.email_enabled:
        body = (
            "Great news from medLegal — your representation agreement (Letter of Representation) "
            "is ready.\n\n"
            + (f"Please review and sign it here:\n{link}\n\n" if link else "")
            + "Reply to this email with any questions.\n\nThank you,\nThe medLegal team"
        )
        await email_service.send_email(
            organization_id, lead_id, lead.email, "Your representation agreement is ready to sign",
            body, purpose="retainer")
    elif to_e164:
        body = "Great news from medLegal — your representation agreement is ready."
        if link:
            body += f" Please review and sign it here: {link}"
        body += " Reply with any questions."
        await messaging_service.send_message(
            organization_id, lead_id, to_e164, body=body, channel=settings.funnel_channel,
            purpose="retainer", content_sid=settings.whatsapp_template_retainer,
            content_vars={"1": "medLegal", "2": link})
    return {"retainer_id": str(row), "link": link, "channel": channel}


async def record_event(
    organization_id: uuid.UUID, retainer_id: uuid.UUID, lead_id: uuid.UUID, event: str,
    *, actor: str = "client", ip: str | None = None, user_agent: str | None = None,
) -> None:
    async with session_scope(system_context(organization_id)) as db:
        await db.execute(
            text("INSERT INTO signature_events (organization_id, retainer_id, lead_id, event, actor, "
                 "ip, user_agent) VALUES (:o,:r,:l,:e,:a,:ip,:ua)"),
            {"o": organization_id, "r": retainer_id, "l": lead_id, "e": event, "a": actor,
             "ip": ip, "ua": user_agent},
        )
        if event == "viewed":
            await db.execute(
                text("UPDATE retainers SET viewed_at=now(), status=CASE WHEN status='Sent' THEN 'Viewed' "
                     "ELSE status END WHERE id=:r"), {"r": retainer_id})
            await db.execute(
                text("UPDATE leads SET retainer_status='Viewed' WHERE id=:l AND retainer_status='Sent'"),
                {"l": lead_id})
        elif event == "signed":
            await db.execute(
                text("UPDATE retainers SET signed_at=now(), status='Signed' WHERE id=:r"), {"r": retainer_id})
            await db.execute(
                text("UPDATE leads SET retainer_status='Signed', pipeline_status='Signed' WHERE id=:l"),
                {"l": lead_id})


async def sign_with_code(code: str, *, ip: str | None = None, user_agent: str | None = None) -> dict:
    resolved = await short_links.resolve(code)
    if resolved is None or resolved["purpose"] != short_links.SIGN:
        raise ValueError("invalid or expired link")
    org, lead_id = resolved["organization_id"], resolved["lead_id"]
    async with session_scope(system_context(org)) as db:
        rid = (await db.execute(
            text("SELECT id FROM retainers WHERE lead_id=:l AND deleted_at IS NULL LIMIT 1"),
            {"l": lead_id})).scalar_one_or_none()
    if rid is None:
        raise ValueError("no retainer to sign")
    await record_event(org, rid, lead_id, "viewed", actor="client", ip=ip, user_agent=user_agent)
    await record_event(org, rid, lead_id, "signed", actor="client", ip=ip, user_agent=user_agent)
    return {"retainer_id": str(rid), "status": "Signed"}
