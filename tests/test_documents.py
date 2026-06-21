"""Wave 3 — document gathering via WhatsApp: request, tokenized upload, in-chat media."""

from __future__ import annotations

import uuid

import httpx
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text

from app.config import settings
from app.main import app
from app.services import document_service, messaging_service, short_links
from app.api.routes import messaging as messaging_route


@pytest_asyncio.fixture(autouse=True)
def mocks(monkeypatch):
    monkeypatch.setattr(settings, "twilio_whatsapp_number", "+14155238886")
    monkeypatch.setattr(settings, "frontend_base_url", "http://localhost:3000")
    monkeypatch.setattr(settings, "public_base_url", None)  # use frontend link in tests
    monkeypatch.setattr(settings, "funnel_channel", "whatsapp")  # deterministic, ignore .env
    monkeypatch.setattr(document_service, "_store_object",
                        lambda path, content, mime: f"gs://test-bucket/{path}")
    monkeypatch.setattr(messaging_service, "_twilio_send",
                        lambda **kw: ("WA_" + uuid.uuid4().hex[:8], "queued"))


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def lead(owner_engine):
    org, lead_id = uuid.uuid4(), uuid.uuid4()
    phone = "+1" + f"{uuid.uuid4().int % 10**10:010d}"
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": org, "s": f"doc-{org.hex[:8]}"})
        await c.execute(text("INSERT INTO leads (id,organization_id,full_name,phone,case_type,source) "
                             "VALUES (:i,:o,'John Doe',:p,'Auto Accident','inbound_call')"),
                        {"i": lead_id, "o": org, "p": phone})
    yield {"org": org, "lead_id": lead_id, "phone": phone}
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": org})


async def test_request_documents_creates_requests(lead, owner_engine):
    res = await document_service.request_documents(lead["org"], lead["lead_id"])
    assert res["requested"]  # default Auto Accident set
    async with owner_engine.begin() as c:
        rows = (await c.execute(text("SELECT status, requested_via FROM document_requests WHERE lead_id=:l"),
                                {"l": lead["lead_id"]})).all()
        missing = (await c.execute(text("SELECT missing_documents FROM leads WHERE id=:l"),
                                   {"l": lead["lead_id"]})).scalar_one()
        msg = (await c.execute(text("SELECT channel, purpose FROM messages WHERE lead_id=:l"),
                               {"l": lead["lead_id"]})).first()
    assert rows and all(r.status == "Sent" and r.requested_via == "whatsapp" for r in rows)
    assert missing == len(rows)
    assert msg.channel == "whatsapp" and msg.purpose == "doc_request"


async def test_code_upload_endpoint(client, lead, owner_engine):
    await document_service.request_documents(lead["org"], lead["lead_id"], doc_types=["Medical records"])
    code = await short_links.create(lead["org"], lead["lead_id"], short_links.UPLOAD)
    r = await client.post("/api/documents/upload", data={"code": code},
                          files={"file": ("records.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert r.status_code == 200, r.text
    async with owner_engine.begin() as c:
        doc = (await c.execute(text("SELECT uploaded_by, scan_status, document_request_id "
                                    "FROM documents WHERE lead_id=:l"), {"l": lead["lead_id"]})).first()
        req = (await c.execute(text("SELECT status FROM document_requests WHERE lead_id=:l AND document_type='Medical records'"),
                               {"l": lead["lead_id"]})).scalar_one()
    # Generic upload satisfies the oldest outstanding request.
    assert doc.uploaded_by == "client" and doc.scan_status == "clean" and doc.document_request_id is not None
    assert req == "Received"
    bad = await client.post("/api/documents/upload", data={"code": "nope"},
                            files={"file": ("x.pdf", b"x", "application/pdf")})
    assert bad.status_code == 401


async def test_whatsapp_inbound_media_ingested(client, lead, owner_engine, monkeypatch):
    monkeypatch.setattr(messaging_route, "_download_twilio_media", lambda url: b"\xff\xd8\xff fake jpeg")
    r = await client.post("/api/messaging/whatsapp/inbound", data={
        "From": f"whatsapp:{lead['phone']}", "Body": "here are my photos",
        "MessageSid": "WA" + uuid.uuid4().hex, "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.com/media/abc", "MediaContentType0": "image/jpeg",
    })
    assert r.status_code == 200
    async with owner_engine.begin() as c:
        inbound = (await c.execute(text("SELECT direction FROM messages WHERE lead_id=:l AND direction='inbound'"),
                                   {"l": lead["lead_id"]})).first()
        doc = (await c.execute(text("SELECT uploaded_by, mime_type FROM documents WHERE lead_id=:l"),
                               {"l": lead["lead_id"]})).first()
    assert inbound is not None
    assert doc is not None and doc.uploaded_by == "client" and doc.mime_type == "image/jpeg"


async def test_short_link_upload_page(client, lead):
    code = await short_links.create(lead["org"], lead["lead_id"], short_links.UPLOAD)
    r = await client.get(f"/u/{code}")
    assert r.status_code == 200 and "Upload your documents" in r.text
    bad = await client.get("/u/nope")
    assert bad.status_code == 404


async def test_sms_channel_document_request(lead, owner_engine, monkeypatch):
    """FUNNEL_CHANNEL=sms sends document requests over SMS (no template needed)."""
    monkeypatch.setattr(settings, "funnel_channel", "sms")
    # SMS sends from the firm's provisioned phone number.
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO phone_numbers (organization_id,e164,is_primary) "
                             "VALUES (:o,:e,true)"), {"o": lead["org"], "e": lead["phone"]})
    await document_service.request_documents(lead["org"], lead["lead_id"], doc_types=["Police report"])
    async with owner_engine.begin() as c:
        msg = (await c.execute(text("SELECT channel, purpose FROM messages WHERE lead_id=:l"),
                               {"l": lead["lead_id"]})).first()
        req = (await c.execute(text("SELECT requested_via FROM document_requests WHERE lead_id=:l"),
                               {"l": lead["lead_id"]})).scalar_one()
    assert msg.channel == "sms" and msg.purpose == "doc_request"
    assert req == "sms"
