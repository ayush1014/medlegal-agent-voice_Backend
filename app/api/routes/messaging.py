"""Inbound WhatsApp webhook (Twilio).

A client reply lands here: we log it to the comms thread, and any attached media
(photos/PDFs of their documents) is ingested into GCS as a document. The lead is
resolved by the sender's phone (single shared WhatsApp sender across firms).
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.config import settings
from app.services import document_service, messaging_service

router = APIRouter(prefix="/messaging", tags=["messaging"])


def _download_twilio_media(url: str) -> bytes | None:
    """Fetch Twilio-hosted media (basic auth). Isolated for test mocking."""
    import httpx

    r = httpx.get(url, auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                  follow_redirects=True, timeout=30)
    return r.content if r.status_code < 400 else None


@router.post("/whatsapp/inbound")
async def whatsapp_inbound(request: Request) -> Response:
    form = dict(await request.form())
    caller = (form.get("From") or "").replace("whatsapp:", "").strip()
    body = form.get("Body")
    sid = form.get("MessageSid") or form.get("SmsMessageSid")
    num_media = int(form.get("NumMedia") or 0)

    resolved = await messaging_service.resolve_lead_by_phone(caller) if caller else None
    if resolved:
        org, lead_id = resolved
        media = [{"url": form.get(f"MediaUrl{i}"), "content_type": form.get(f"MediaContentType{i}")}
                 for i in range(num_media) if form.get(f"MediaUrl{i}")]
        await messaging_service.record_inbound(
            org, lead_id, channel="whatsapp", body=body, media=media or None, provider_message_id=sid)
        for i in range(num_media):
            url = form.get(f"MediaUrl{i}")
            ctype = form.get(f"MediaContentType{i}")
            if not url:
                continue
            content = _download_twilio_media(url)
            if content:
                ext = (ctype or "application/octet-stream").split("/")[-1][:8] or "bin"
                await document_service.record_upload(
                    org, lead_id, file_name=f"whatsapp_{sid or 'msg'}_{i}.{ext}",
                    content=content, mime=ctype, uploaded_by="client")

    return Response(content="<Response></Response>", media_type="application/xml")
