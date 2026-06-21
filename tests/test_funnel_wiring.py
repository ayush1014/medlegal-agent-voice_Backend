"""Wave 0 — funnel wiring: outbox publisher dispatch + WhatsApp messaging."""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.database import session_scope
from app.security.context import system_context
from app.services import messaging_service, outbox_publisher, outbox_service


def _phone() -> str:
    return "+1" + f"{uuid.uuid4().int % 10**10:010d}"


@pytest_asyncio.fixture
async def org(owner_engine):
    oid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": oid, "s": f"fw-{oid.hex[:8]}"})
    yield oid
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": oid})


async def _make_lead(owner_engine, org) -> uuid.UUID:
    lid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO leads (id,organization_id,full_name,phone,case_type,source) "
                             "VALUES (:i,:o,'John','+15550000000','Auto Accident','inbound_call')"),
                        {"i": lid, "o": org})
    return lid


async def test_outbox_dispatch_runs_handler(org, owner_engine):
    lead_id = await _make_lead(owner_engine, org)
    seen = []

    @outbox_publisher.on("test.dispatch")
    async def _handler(db, organization_id, aggregate_id, payload):
        seen.append((organization_id, aggregate_id, payload.get("k")))

    async with session_scope(system_context(org)) as db:
        await outbox_service.emit_event(
            db, org, aggregate_type="lead", aggregate_id=lead_id,
            event_type="test.dispatch", payload={"k": "v"},
        )

    res = await outbox_publisher.dispatch_pending_for_org(org)
    assert res["published"] == 1
    assert seen and seen[0][1] == lead_id and seen[0][2] == "v"

    async with owner_engine.begin() as c:
        status = (await c.execute(
            text("SELECT status FROM outbox_events WHERE aggregate_id=:l AND event_type='test.dispatch'"),
            {"l": lead_id})).scalar_one()
    assert status == "published"
    # cleanup the module-global handler registration
    outbox_publisher._HANDLERS.get("test.dispatch", []).clear()


async def test_whatsapp_send_persists_message(org, owner_engine, monkeypatch):
    monkeypatch.setattr(settings, "twilio_whatsapp_number", "+14155238886")
    monkeypatch.setattr(messaging_service, "_twilio_send",
                        lambda **kw: ("WA_" + uuid.uuid4().hex[:8], "queued"))
    lead_id = await _make_lead(owner_engine, org)

    pid = await messaging_service.send_message(
        org, lead_id, _phone(), body="Hi from medLegal", channel="whatsapp", purpose="follow_up"
    )
    assert pid and pid.startswith("WA_")
    async with owner_engine.begin() as c:
        row = (await c.execute(text("SELECT channel, direction, purpose, status FROM messages WHERE lead_id=:l"),
                               {"l": lead_id})).first()
        conv = (await c.execute(text("SELECT channel FROM conversations WHERE lead_id=:l"),
                                {"l": lead_id})).scalar_one()
    assert row.channel == "whatsapp" and row.direction == "outbound" and row.status == "queued"
    assert conv == "whatsapp"


async def test_record_inbound_whatsapp(org, owner_engine):
    lead_id = await _make_lead(owner_engine, org)
    await messaging_service.record_inbound(
        org, lead_id, channel="whatsapp", body="here are my photos",
        media=[{"url": "https://x/y.jpg", "content_type": "image/jpeg"}],
        provider_message_id="WAin_" + uuid.uuid4().hex[:8],
    )
    async with owner_engine.begin() as c:
        row = (await c.execute(text("SELECT direction, body, media FROM messages WHERE lead_id=:l"),
                               {"l": lead_id})).first()
    assert row.direction == "inbound" and "photos" in row.body
    assert row.media is not None
