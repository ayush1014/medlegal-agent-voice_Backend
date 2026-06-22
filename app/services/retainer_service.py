"""Retainer / Letter of Representation (LOR) closing.

Generates the LOR, stores it, and sends the client a WhatsApp link to an internal
(mock) e-sign page. Signing records the append-only signature_events trail and
advances the lead to Signed. Clean seam to swap in DocuSign/Dropbox Sign later.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import session_scope
from app.security.context import system_context
from app.config import settings
from app.services import document_service, email_service, messaging_service, pdf_service, short_links

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
    """Magic-link the client clicks to review + sign the LOR — points at the Vercel
    frontend `/sign/{code}` (no login; the single-purpose token IS the auth). Falls back
    to the backend page if no frontend URL is configured. Empty if neither is set."""
    code = await short_links.create(org, lead_id, short_links.SIGN)
    fe = (settings.frontend_base_url or "").rstrip("/")
    if fe:
        return f"{fe}/sign/{code}"
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/u/{code}" if base else ""


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
    sent_to: str | None = None
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
        sent_to = lead.email
    elif to_e164:
        body = "Great news from medLegal — your representation agreement is ready."
        if link:
            body += f" Please review and sign it here: {link}"
        body += " Reply with any questions."
        await messaging_service.send_message(
            organization_id, lead_id, to_e164, body=body, channel=settings.funnel_channel,
            purpose="retainer", content_sid=settings.whatsapp_template_retainer,
            content_vars={"1": "medLegal", "2": link})
        sent_to = to_e164
    return {"retainer_id": str(row), "link": link, "channel": channel, "sent_to": sent_to}


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


async def lor_view(code: str) -> dict:
    """Resolve a sign magic-link → the LOR to render on the frontend (records a 'viewed').
    Raises ValueError on an invalid/expired code."""
    resolved = await short_links.resolve(code)
    if resolved is None or resolved["purpose"] != short_links.SIGN:
        raise ValueError("invalid or expired link")
    org, lead_id = resolved["organization_id"], resolved["lead_id"]
    async with session_scope(system_context(org)) as db:
        lead = (await db.execute(
            text("SELECT full_name, case_type FROM leads WHERE id=:l"), {"l": lead_id})).first()
        firm = (await db.execute(
            text("SELECT name FROM organizations WHERE id=:o"), {"o": org})).scalar_one_or_none()
        ret = (await db.execute(
            text("SELECT id, status, signer_name FROM retainers WHERE lead_id=:l AND deleted_at IS NULL "
                 "LIMIT 1"), {"l": lead_id})).first()
    if lead is None or ret is None:
        raise ValueError("no retainer to sign")
    firm = firm or "medLegal"
    signed = ret.status == "Signed"
    if not signed:
        await record_event(org, ret.id, lead_id, "viewed", actor="client")
    return {
        "firm": firm, "client_name": lead.full_name, "case_type": lead.case_type,
        "lor_text": _lor_text(firm, lead.full_name, lead.case_type),
        "status": ret.status, "signed": signed, "signer_name": ret.signer_name,
    }


async def finalize_sign(
    org: uuid.UUID, lead_id: uuid.UUID, retainer_id: uuid.UUID, signer_name: str | None,
    *, ip: str | None = None, user_agent: str | None = None,
) -> dict:
    """Record the signature, generate the signed-LOR PDF, store it, and email the client
    an acknowledgment with the PDF attached. Shared by the magic-link + portal sign paths."""
    async with session_scope(system_context(org)) as db:
        lead = (await db.execute(
            text("SELECT full_name, case_type, email FROM leads WHERE id=:l"), {"l": lead_id})).first()
        firm = (await db.execute(
            text("SELECT name FROM organizations WHERE id=:o"), {"o": org})).scalar_one_or_none() or "medLegal"
    client_name = (lead.full_name if lead else None) or "Client"
    signer_name = (signer_name or client_name).strip() or client_name

    await record_event(org, retainer_id, lead_id, "signed", actor="client", ip=ip, user_agent=user_agent)

    signed_at = datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC")
    lor = _lor_text(firm, client_name, lead.case_type if lead else "")
    pdf = pdf_service.lor_pdf(firm=firm, client_name=client_name, lor_text=lor,
                             signer_name=signer_name, signed_at=signed_at, ip=ip)
    path = f"{org}/{lead_id}/signed_lor_{uuid.uuid4().hex}.pdf"
    pdf_url = document_service._store_object(path, pdf, "application/pdf")

    async with session_scope(system_context(org)) as db:
        await db.execute(
            text("UPDATE retainers SET document_url=:u, signer_name=:n WHERE id=:r"),
            {"u": pdf_url, "n": signer_name, "r": retainer_id})
        await db.execute(
            text("INSERT INTO documents (id, organization_id, lead_id, file_name, storage_url, mime_type, "
                 "size_bytes, uploaded_by, scan_status, doc_category, doc_summary, match_status) "
                 "VALUES (:id,:o,:l,:fn,:url,'application/pdf',:sz,'client','clean','retainer_signed',:sum,'matched')"),
            {"id": uuid.uuid4(), "o": org, "l": lead_id, "fn": "Letter-of-Representation-signed.pdf",
             "url": pdf_url, "sz": len(pdf), "sum": f"Signed Letter of Representation — {signer_name}"})

    if lead and lead.email:
        body = (
            f"Thank you, {signer_name} — your Letter of Representation with {firm} is now signed.\n\n"
            "A copy of the fully-signed agreement is attached for your records.\n\n"
            "Welcome aboard,\nThe medLegal team"
        )
        await email_service.send_email(
            org, lead_id, lead.email, "Your signed Letter of Representation", body,
            purpose="retainer",
            attachments=[("Letter-of-Representation-signed.pdf", pdf, "application/pdf")])
    return {"retainer_id": str(retainer_id), "status": "Signed", "pdf_url": pdf_url}


async def sign_via_code(
    code: str, signer_name: str | None = None, *, ip: str | None = None, user_agent: str | None = None
) -> dict:
    """Sign through a magic-link code (the emailed flow): validate → finalize."""
    resolved = await short_links.resolve(code)
    if resolved is None or resolved["purpose"] != short_links.SIGN:
        raise ValueError("invalid or expired link")
    org, lead_id = resolved["organization_id"], resolved["lead_id"]
    async with session_scope(system_context(org)) as db:
        ret = (await db.execute(
            text("SELECT id, status FROM retainers WHERE lead_id=:l AND deleted_at IS NULL LIMIT 1"),
            {"l": lead_id})).first()
    if ret is None:
        raise ValueError("no retainer to sign")
    if ret.status == "Signed":
        return {"retainer_id": str(ret.id), "status": "Signed", "already": True}
    return await finalize_sign(org, lead_id, ret.id, signer_name, ip=ip, user_agent=user_agent)


# Backwards-compatible alias for the backend form page.
async def sign_with_code(code: str, *, ip: str | None = None, user_agent: str | None = None) -> dict:
    return await sign_via_code(code, None, ip=ip, user_agent=user_agent)
