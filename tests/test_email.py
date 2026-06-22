"""Email layer (Gmail SMTP outbound + IMAP inbound) — SMTP/IMAP/GCS all mocked."""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.jobs import email_inbound
from app.services import document_service, email_service


@pytest_asyncio.fixture
async def org(owner_engine):
    oid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": oid, "s": f"em-{oid.hex[:8]}"})
    yield oid
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": oid})


async def _lead(owner_engine, org, *, email="client@example.com") -> uuid.UUID:
    lid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO leads (id,organization_id,full_name,phone,email,case_type,source) "
                             "VALUES (:i,:o,'Jane Roe','+15551112222',:e,'Auto Accident','inbound_call')"),
                        {"i": lid, "o": org, "e": email})
    return lid


def _enable_email(monkeypatch):
    monkeypatch.setattr(settings, "gmail_user", "firm@gmail.com")
    monkeypatch.setattr(settings, "gmail_app_password", "app-pass")


async def test_send_email_records_message(org, owner_engine, monkeypatch):
    _enable_email(monkeypatch)
    monkeypatch.setattr(email_service, "_send_sync",
                        lambda to, s, b, r, attachments=None: "<mid-1@test>")
    lead_id = await _lead(owner_engine, org)

    mid = await email_service.send_email(org, lead_id, "client@example.com", "Subj", "Body here",
                                         purpose="doc_request")
    assert mid == "<mid-1@test>"
    async with owner_engine.begin() as c:
        row = (await c.execute(text("SELECT channel, direction, purpose, status FROM messages "
                                    "WHERE lead_id=:l"), {"l": lead_id})).mappings().first()
    assert row["channel"] == "email" and row["direction"] == "outbound"
    assert row["purpose"] == "doc_request" and row["status"] == "sent"


async def test_doc_request_goes_via_email(org, owner_engine, monkeypatch):
    _enable_email(monkeypatch)
    sent = {}
    def _fake_send(to, s, b, r, attachments=None):
        sent["to"] = to; sent["body"] = b
        return "<mid-2@test>"
    monkeypatch.setattr(email_service, "_send_sync", _fake_send)
    lead_id = await _lead(owner_engine, org, email="docs@example.com")

    res = await document_service.request_documents(org, lead_id, doc_types=["Police report", "Medical bills"])
    assert res["channel"] == "email" and res["created"] == 2
    assert sent["to"] == "docs@example.com" and "Police report" in sent["body"]
    async with owner_engine.begin() as c:
        via = (await c.execute(text("SELECT DISTINCT requested_via FROM document_requests WHERE lead_id=:l"),
                               {"l": lead_id})).scalars().all()
        msg = (await c.execute(text("SELECT channel, purpose FROM messages WHERE lead_id=:l"),
                               {"l": lead_id})).mappings().first()
    assert via == ["email"]
    assert msg["channel"] == "email" and msg["purpose"] == "doc_request"


async def test_inbound_ingests_attachments(org, owner_engine, monkeypatch):
    _enable_email(monkeypatch)
    lead_id = await _lead(owner_engine, org, email="reply@example.com")
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO document_requests (organization_id,lead_id,document_type,status,"
                             "requested_via,requested_at) VALUES (:o,:l,'Police report','Sent','email',now())"),
                        {"o": org, "l": lead_id})

    monkeypatch.setattr(document_service, "_store_object", lambda p, c, m: f"gs://bucket/{p}")
    monkeypatch.setattr(email_inbound, "_mark_seen", lambda uids: None)
    monkeypatch.setattr(email_inbound, "_fetch_matching", lambda lead_emails: [{
        "uid": b"1", "sender": "reply@example.com", "subject": "my docs",
        "message_id": "<in-1@example.com>",
        "attachments": [("police_report.pdf", b"%PDF-1.4 data", "application/pdf")],
    }])

    res = await email_inbound.poll_inbound()
    assert res["emails"] == 1 and res["files"] == 1
    async with owner_engine.begin() as c:
        n_docs = (await c.execute(text("SELECT count(*) FROM documents WHERE lead_id=:l"),
                                  {"l": lead_id})).scalar_one()
        req_status = (await c.execute(text("SELECT status FROM document_requests WHERE lead_id=:l"),
                                      {"l": lead_id})).scalar_one()
        inbound = (await c.execute(text("SELECT count(*) FROM messages WHERE lead_id=:l "
                                        "AND direction='inbound'"), {"l": lead_id})).scalar_one()
        # Decoupled flow: ingest stores the file + emits document.received for the worker to
        # classify + content-match. The request is NOT blind-marked Received on ingest.
        events = (await c.execute(text(
            "SELECT count(*) FROM outbox_events WHERE event_type='document.received' "
            "AND aggregate_id IN (SELECT id FROM documents WHERE lead_id=:l)"),
            {"l": lead_id})).scalar_one()
    assert n_docs == 1 and inbound == 1
    assert req_status == "Sent" and events == 1
