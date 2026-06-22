"""Document gathering over WhatsApp.

Two ingestion paths (the user chose both): a tokenized portal upload link sent
over WhatsApp, AND files sent directly in the WhatsApp thread. Both land in GCS
with a `documents` row, mark the matching request Received, and keep the lead's
`missing_documents` rollup fresh. The GCS write (`_store_object`) is isolated so
tests mock it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import session_scope
from app.security.context import system_context
from app.services import email_service, messaging_service, short_links

# Sensible default asks per case type (subset of REQUESTABLE_DOCUMENTS).
_DEFAULT_DOCS = {
    "Auto Accident": ["Police report", "Medical records", "Medical bills",
                      "Insurance correspondence", "Vehicle damage photos"],
    "Slip and Fall": ["Accident photos", "Medical records", "Medical bills", "Witness information"],
    "Dog Bite": ["Injury photos", "Medical records", "Medical bills"],
    "_default": ["Medical records", "Medical bills", "Accident photos", "Insurance correspondence"],
}


def default_docs_for(case_type: str) -> list[str]:
    return _DEFAULT_DOCS.get(case_type, _DEFAULT_DOCS["_default"])


def _store_object(path: str, content: bytes, mime: str | None) -> str:
    """Upload bytes to GCS; return the object reference. Mocked in tests."""
    from app.services.storage import get_bucket

    bucket = get_bucket()
    blob = bucket.blob(path)
    blob.upload_from_string(content, content_type=mime or "application/octet-stream")
    return f"gs://{settings.gcs_bucket_name}/{path}"


async def upload_link(org: uuid.UUID, lead_id: uuid.UUID) -> str:
    """A short, clickable upload URL served by the backend (reachable via the
    public tunnel). Empty string if no public base URL is configured."""
    base = (settings.public_base_url or "").rstrip("/")
    if not base:
        return ""
    code = await short_links.create(org, lead_id, short_links.UPLOAD)
    return f"{base}/u/{code}"


async def _recompute_missing(db: AsyncSession, lead_id: uuid.UUID) -> int:
    n = (await db.execute(
        text("SELECT count(*) FROM document_requests WHERE lead_id=:l "
             "AND status NOT IN ('Received','Waived')"), {"l": lead_id}
    )).scalar_one()
    await db.execute(text("UPDATE leads SET missing_documents=:n WHERE id=:l"), {"n": n, "l": lead_id})
    return n


async def request_documents(
    organization_id: uuid.UUID, lead_id: uuid.UUID, to_e164: str | None = None,
    doc_types: list[str] | None = None, *, channel: str | None = None,
) -> dict:
    """Create document requests and send the client an ask + upload link."""
    async with session_scope(system_context(organization_id)) as db:
        row = (await db.execute(
            text("SELECT case_type, phone, email FROM leads WHERE id=:l"), {"l": lead_id})).first()
        case_type = row.case_type if row else None
        to_e164 = to_e164 or (row.phone if row else None)
        email = row.email if row else None
        # Prefer EMAIL (our doc-intake channel) when we have an address; else SMS/WhatsApp.
        if channel is None:
            channel = "email" if (email and settings.email_enabled) else settings.funnel_channel
        docs = doc_types or default_docs_for(case_type or "_default")
        created = 0
        for dt in docs:
            existing = (await db.execute(
                text("SELECT id FROM document_requests WHERE lead_id=:l AND document_type=:dt "
                     "AND status NOT IN ('Received','Waived') LIMIT 1"),
                {"l": lead_id, "dt": dt})).scalar_one_or_none()
            if existing:
                continue
            await db.execute(
                text("INSERT INTO document_requests (organization_id, lead_id, document_type, status, "
                     "requested_via, requested_at) VALUES (:o,:l,:dt,'Sent',:ch, now())"),
                {"o": organization_id, "l": lead_id, "dt": dt, "ch": channel},
            )
            created += 1
        # Keep outstanding requests' channel in sync with how we're actually sending.
        await db.execute(
            text("UPDATE document_requests SET requested_via=:ch WHERE lead_id=:l "
                 "AND status NOT IN ('Received','Waived')"), {"ch": channel, "l": lead_id})
        missing = await _recompute_missing(db, lead_id)

    link = await upload_link(organization_id, lead_id)
    checklist = ", ".join(docs)          # single line — used for SMS/WhatsApp
    bullets = "\n".join(f"• {d}" for d in docs)  # bulleted — used for email
    sent_to: str | None = None
    # Send whenever there are outstanding (not-yet-received) asks — so a deliberate
    # "Request Documents" click always (re)sends the email, and the auto post-call
    # request fires once. (Nudges are a separate reminder path.)
    if missing > 0 and channel == "email" and email:
        # No raw link in the email body — an IP-literal URL is a strong spam signal,
        # and clients send docs by replying with attachments anyway.
        body = (
            "Hi, this is medLegal. To move your case forward, please send us these documents:\n\n"
            f"{bullets}\n\n"
            "Just reply to this email with the photos or PDFs attached.\n\n"
            "Thank you,\nThe medLegal team"
        )
        await email_service.send_email(
            organization_id, lead_id, email, "Documents needed for your case", body,
            purpose="doc_request")
        sent_to = email
    elif missing > 0 and to_e164:
        body = f"Hi, it's medLegal. To move your case forward we need a few documents: {checklist}."
        if link:
            body += f" Upload them securely here: {link}"
        if channel == "whatsapp":  # in-chat photo replies only work on WhatsApp
            body += " — or just reply to this message with photos."
        await messaging_service.send_message(
            organization_id, lead_id, to_e164, body=body, channel=channel, purpose="doc_request",
            content_sid=settings.whatsapp_template_doc_request,
            content_vars={"1": "medLegal", "2": checklist, "3": link},
        )
        sent_to = to_e164
    return {"requested": docs, "created": created, "missing": missing, "link": link,
            "channel": channel, "sent_to": sent_to}


async def record_upload(
    organization_id: uuid.UUID, lead_id: uuid.UUID, *, file_name: str, content: bytes,
    mime: str | None = None, uploaded_by: str = "client", document_request_id: uuid.UUID | None = None,
    document_type: str | None = None,
) -> uuid.UUID:
    """Persist an uploaded/received file to GCS + documents, matching it to a
    pending request (by id or document_type) and refreshing the rollup."""
    path = f"{organization_id}/{lead_id}/{uuid.uuid4().hex}_{file_name}"
    storage_url = _store_object(path, content, mime)

    doc_id = uuid.uuid4()
    async with session_scope(system_context(organization_id)) as db:
        req_id = document_request_id
        if req_id is None and document_type:
            req_id = (await db.execute(
                text("SELECT id FROM document_requests WHERE lead_id=:l AND document_type=:dt "
                     "AND status NOT IN ('Received','Waived') ORDER BY created_at LIMIT 1"),
                {"l": lead_id, "dt": document_type})).scalar_one_or_none()
        if req_id is None:
            # Generic upload (no type tagged): satisfy the oldest outstanding ask so
            # the checklist + missing_documents rollup advance the funnel.
            req_id = (await db.execute(
                text("SELECT id FROM document_requests WHERE lead_id=:l "
                     "AND status NOT IN ('Received','Waived') ORDER BY created_at LIMIT 1"),
                {"l": lead_id})).scalar_one_or_none()
        await db.execute(
            text("INSERT INTO documents (id, organization_id, lead_id, document_request_id, file_name, "
                 "storage_url, mime_type, size_bytes, uploaded_by, scan_status) "
                 "VALUES (:id,:o,:l,:rid,:fn,:url,:mime,:sz,:by,'clean')"),
            {"id": doc_id, "o": organization_id, "l": lead_id, "rid": req_id, "fn": file_name,
             "url": storage_url, "mime": mime, "sz": len(content), "by": uploaded_by},
        )
        if req_id is not None:
            await db.execute(
                text("UPDATE document_requests SET status='Received' WHERE id=:rid"), {"rid": req_id})
        await _recompute_missing(db, lead_id)
    return doc_id
