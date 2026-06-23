"""Inbound email poller — ingest client document replies into GCS.

Clients reply to our doc-request email with photos/PDFs attached; this polls the
firm Gmail over IMAP, and for messages **from a known lead's email address** pulls
the attachments → GCS (via document_service.record_upload) and logs the inbound on
the timeline. Everything else in the inbox is left completely untouched — we only
peek headers, and we only mark-as-read the messages we actually ingest. (Cleaner
with a dedicated Gmail; safe on a personal one.)

imaplib is blocking, so all IMAP work runs in worker threads.
"""

from __future__ import annotations

import email
import imaplib
import logging
import uuid
from email.header import decode_header
from email.utils import parseaddr

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import NEON_CONNECT_ARGS, _build_async_url
from app.services import document_service, email_service

logger = logging.getLogger("medlegal.email.inbound")

_MAX_PER_POLL = 50
_ALLOWED_MIME = ("image/", "application/pdf")
# Also re-scan recently-read mail this many days back, so a client reply that was opened
# in the inbox (marked \Seen) before the poller ran still gets ingested. De-duped by
# Message-ID against what's already been recorded, so nothing is ingested twice.
_LOOKBACK_DAYS = 14


def _since_date() -> str:
    """IMAP SINCE wants DD-Mon-YYYY (this is a normal module — datetime.now is fine here)."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)).strftime("%d-%b-%Y")


def _decode(s: str | None) -> str:
    if not s:
        return ""
    return "".join(
        (part.decode(enc or "utf-8", "replace") if isinstance(part, bytes) else part)
        for part, enc in decode_header(s)
    )


def _attachments(msg) -> list[tuple[str, bytes, str]]:
    out: list[tuple[str, bytes, str]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        mime = (part.get_content_type() or "").lower()
        if not mime.startswith(_ALLOWED_MIME):
            continue
        filename = _decode(part.get_filename())
        # Real attachments AND inline image/PDF parts that carry a filename. Gmail and
        # phone mail clients attach reply photos *inline* (Content-Disposition: inline),
        # not as "attachment" — requiring "attachment" silently dropped client documents.
        if part.get_content_disposition() != "attachment" and not filename:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        out.append((filename or f"upload_{uuid.uuid4().hex}", payload, mime))
    return out


def _fetch_matching(lead_emails: set[str], already_ids: set[str]) -> list[dict]:
    """Parsed messages from known lead addresses with attachments — UNSEEN plus a recent
    lookback window (so already-read replies are still ingested), de-duped by Message-ID
    against what's already recorded. Non-client mail is only header-peeked, never touched."""
    matches: list[dict] = []
    M = imaplib.IMAP4_SSL(settings.imap_host)
    try:
        M.login(settings.gmail_user, settings.gmail_app_password)
        M.select("INBOX")
        # New (unseen) + a recent window to catch replies that were opened before we polled.
        uids: list[bytes] = []
        for criteria in (("UNSEEN",), ("SINCE", _since_date())):
            typ, data = M.uid("search", None, *criteria)
            if typ == "OK" and data and data[0]:
                uids += data[0].split()
        ordered, seen = [], set()
        for u in reversed(uids):  # most-recent first, unique
            if u not in seen:
                seen.add(u)
                ordered.append(u)
        for uid in ordered[:_MAX_PER_POLL]:
            typ, hdr = M.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])")
            if typ != "OK" or not hdr or not hdr[0]:
                continue
            head = email.message_from_bytes(hdr[0][1])
            # Stable id for de-dup: prefer Message-ID, else a UID-derived fallback (NOT random,
            # so a Message-ID-less mail still de-dups across polls instead of re-ingesting).
            msg_id = (head.get("Message-ID") or "").strip() or f"<uid-{uid.decode(errors='ignore')}@inbound>"
            if msg_id in already_ids:
                continue  # already ingested — don't touch it
            sender = parseaddr(head.get("From", ""))[1].lower()
            if sender not in lead_emails:
                continue  # not a client — leave it untouched
            typ, full = M.uid("fetch", uid, "(BODY.PEEK[])")  # peek so we control the read flag
            if typ != "OK" or not full or not full[0]:
                continue
            msg = email.message_from_bytes(full[0][1])
            atts = _attachments(msg)
            if not atts:
                continue
            matches.append({
                "uid": uid, "sender": sender,
                "subject": _decode(head.get("Subject")),
                "message_id": msg_id,
                "attachments": atts,
            })
        return matches
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass


async def _ingested_message_ids() -> set[str]:
    """Message-IDs of inbound emails already recorded, so re-scans never double-ingest."""
    engine = create_async_engine(_build_async_url(settings.database_url), poolclass=NullPool,
                                 connect_args=NEON_CONNECT_ARGS)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT provider_message_id FROM messages WHERE direction='inbound' "
                "AND channel='email' AND provider_message_id IS NOT NULL"))).all()
    finally:
        await engine.dispose()
    return {r[0] for r in rows}


def _mark_seen(uids: list[bytes]) -> None:
    if not uids:
        return
    M = imaplib.IMAP4_SSL(settings.imap_host)
    try:
        M.login(settings.gmail_user, settings.gmail_app_password)
        M.select("INBOX")
        for uid in uids:
            M.uid("store", uid, "+FLAGS", "(\\Seen)")
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass


async def _lead_emails() -> dict[str, tuple[uuid.UUID, uuid.UUID]]:
    """{lower(email): (organization_id, lead_id)} for leads with an email on file."""
    engine = create_async_engine(_build_async_url(settings.database_url), poolclass=NullPool, connect_args=NEON_CONNECT_ARGS)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT lower(email) AS email, id, organization_id FROM leads "
                "WHERE email IS NOT NULL AND deleted_at IS NULL ORDER BY created_at"))).all()
    finally:
        await engine.dispose()
    # later rows (more recent) win on duplicate emails
    return {r.email: (r.organization_id, r.id) for r in rows if r.email}


async def poll_inbound() -> dict:
    """One inbound sweep. Returns {emails, files}."""
    import asyncio

    result = {"emails": 0, "files": 0}
    if not settings.email_enabled:
        return result
    lead_emails = await _lead_emails()
    if not lead_emails:
        return result

    already = await _ingested_message_ids()
    matches = await asyncio.to_thread(_fetch_matching, set(lead_emails), already)
    done_uids: list[bytes] = []
    for m in matches:
        org, lead_id = lead_emails[m["sender"]]
        try:
            for name, content, mime in m["attachments"]:
                await document_service.record_upload(
                    org, lead_id, file_name=name, content=content, mime=mime, uploaded_by="client")
                result["files"] += 1
            await email_service.record_inbound(
                org, lead_id, body=f"[email] {m['subject']} — {len(m['attachments'])} attachment(s)",
                provider_id=m["message_id"])
            result["emails"] += 1
            done_uids.append(m["uid"])
            logger.info("ingested %d file(s) from %s → lead %s", len(m["attachments"]), m["sender"], lead_id)
        except Exception:  # noqa: BLE001 - leave this email UNREAD so the next poll retries
            logger.exception("failed ingesting email from %s (lead %s)", m["sender"], lead_id)

    if done_uids:
        await asyncio.to_thread(_mark_seen, done_uids)
    return result
