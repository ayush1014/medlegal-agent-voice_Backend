"""Wave 4/5 — retainer/LOR e-sign + follow-up automation transitions."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.services import (
    document_service, followup_service, messaging_service, retainer_service, short_links,
)

NOW = datetime(2026, 6, 21, tzinfo=timezone.utc)


@pytest_asyncio.fixture(autouse=True)
def mocks(monkeypatch):
    monkeypatch.setattr(settings, "twilio_whatsapp_number", "+14155238886")
    monkeypatch.setattr(settings, "frontend_base_url", "http://localhost:3000")
    monkeypatch.setattr(settings, "funnel_channel", "whatsapp")  # deterministic, ignore .env
    monkeypatch.setattr(document_service, "_store_object",
                        lambda path, content, mime: f"gs://test/{path}")
    monkeypatch.setattr(messaging_service, "_twilio_send",
                        lambda **kw: ("WA_" + uuid.uuid4().hex[:8], "queued"))


@pytest_asyncio.fixture
async def org(owner_engine):
    oid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'Bridgepoint Law',:s)"),
                        {"o": oid, "s": f"rf-{oid.hex[:8]}"})
    yield oid
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": oid})


async def _lead(owner_engine, org, **cols) -> uuid.UUID:
    lid = uuid.uuid4()
    base = dict(full_name="John Doe", phone="+1" + f"{uuid.uuid4().int % 10**10:010d}",
                case_type="Auto Accident", source="inbound_call", qualification_status="Needs Review",
                pipeline_status="Intake Complete", retainer_status="Not Ready", missing_documents=0)
    base.update(cols)
    keys = ",".join(base) + ",organization_id,id"
    vals = ",".join(f":{k}" for k in base) + ",:org,:id"
    async with owner_engine.begin() as c:
        await c.execute(text(f"INSERT INTO leads ({keys}) VALUES ({vals})"),
                        {**base, "org": org, "id": lid})
    return lid


async def test_retainer_send_and_sign(org, owner_engine):
    lead_id = await _lead(owner_engine, org, qualification_status="Qualified", pipeline_status="Docs Received")
    res = await retainer_service.prepare_and_send(org, lead_id)
    retainer_id = uuid.UUID(res["retainer_id"])

    async with owner_engine.begin() as c:
        r = (await c.execute(text("SELECT status, esign_provider, document_url FROM retainers WHERE id=:r"),
                             {"r": retainer_id})).first()
        lead = (await c.execute(text("SELECT retainer_status, pipeline_status FROM leads WHERE id=:l"),
                                {"l": lead_id})).first()
        sent = (await c.execute(text("SELECT count(*) FROM signature_events WHERE retainer_id=:r AND event='sent'"),
                                {"r": retainer_id})).scalar_one()
    assert r.status == "Sent" and r.esign_provider == "internal_mock" and r.document_url
    assert lead.retainer_status == "Sent" and lead.pipeline_status == "Retainer Sent"
    assert sent == 1

    code = await short_links.create(org, lead_id, short_links.SIGN)
    out = await retainer_service.sign_with_code(code, ip="1.2.3.4", user_agent="pytest")
    assert out["status"] == "Signed"
    async with owner_engine.begin() as c:
        r = (await c.execute(text("SELECT status, signed_at FROM retainers WHERE id=:r"), {"r": retainer_id})).first()
        lead = (await c.execute(text("SELECT retainer_status, pipeline_status FROM leads WHERE id=:l"),
                                {"l": lead_id})).first()
        signed = (await c.execute(text("SELECT count(*) FROM signature_events WHERE retainer_id=:r AND event='signed'"),
                                  {"r": retainer_id})).scalar_one()
    assert r.status == "Signed" and r.signed_at is not None
    assert lead.retainer_status == "Signed" and lead.pipeline_status == "Signed" and signed == 1


async def test_followup_transitions(org, owner_engine):
    # L1: Qualified, no docs requested -> should request docs.
    l1 = await _lead(owner_engine, org, qualification_status="Qualified", pipeline_status="Qualified")
    # L2: docs all received -> should send retainer.
    l2 = await _lead(owner_engine, org, qualification_status="Qualified",
                     pipeline_status="Docs Requested", missing_documents=0)
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO document_requests (organization_id,lead_id,document_type,status) "
                             "VALUES (:o,:l,'Medical records','Received')"), {"o": org, "l": l2})
    # L3: docs missing + stale -> should nudge.
    l3 = await _lead(owner_engine, org, qualification_status="Qualified",
                     pipeline_status="Docs Requested", missing_documents=2)
    async with owner_engine.begin() as c:
        await c.execute(text("UPDATE leads SET last_follow_up_at=:t WHERE id=:l"),
                        {"t": NOW - timedelta(days=3), "l": l3})
        await c.execute(text("INSERT INTO document_requests (organization_id,lead_id,document_type,status) "
                             "VALUES (:o,:l,'Police report','Sent')"), {"o": org, "l": l3})

    counts = await followup_service.run_followups(org, now=NOW)
    assert counts["docs_requested"] == 1
    assert counts["retainers_sent"] == 1
    assert counts["doc_nudges"] == 1

    async with owner_engine.begin() as c:
        p1 = (await c.execute(text("SELECT pipeline_status FROM leads WHERE id=:l"), {"l": l1})).scalar_one()
        n1 = (await c.execute(text("SELECT count(*) FROM document_requests WHERE lead_id=:l"), {"l": l1})).scalar_one()
        rstat2 = (await c.execute(text("SELECT retainer_status, pipeline_status FROM leads WHERE id=:l"), {"l": l2})).first()
        nudge3 = (await c.execute(text("SELECT count(*) FROM messages WHERE lead_id=:l AND purpose='doc_request'"),
                                  {"l": l3})).scalar_one()
    assert p1 == "Docs Requested" and n1 >= 1
    assert rstat2.retainer_status == "Sent" and rstat2.pipeline_status == "Retainer Sent"
    assert nudge3 >= 1
