"""Phase G — never-drop-a-lead: voicemail fallback, dropped-call resume SMS,
callback SMS. Twilio sending is mocked (no real texts)."""

from __future__ import annotations

import uuid

import httpx
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text

from app.config import settings
from app.database import session_scope
from app.main import app
from app.security.context import system_context
from app.services import sms_service, voice_service


def _phone() -> str:
    return "+1" + f"{uuid.uuid4().int % 10**10:010d}"


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(autouse=True)
def patches(monkeypatch):
    monkeypatch.setattr(settings, "twilio_validate_webhooks", False)
    monkeypatch.setattr(settings, "livekit_sip_uri", None)  # voicemail fallback path
    # Never hit real Twilio.
    monkeypatch.setattr(sms_service, "_twilio_create_message",
                        lambda f, t, b: ("SM_fake_" + uuid.uuid4().hex[:8], "queued"))


@pytest_asyncio.fixture
async def firm(owner_engine):
    org, dialed = uuid.uuid4(), _phone()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": org, "s": f"fb-{org.hex[:8]}"})
        await c.execute(text("INSERT INTO phone_numbers (organization_id,e164,is_primary) "
                             "VALUES (:o,:e,true)"), {"o": org, "e": dialed})
    yield {"org": org, "dialed": dialed}
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": org})


async def _make_lead(owner_engine, org) -> uuid.UUID:
    lid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO leads (id,organization_id,full_name,phone,case_type,source) "
                             "VALUES (:i,:o,'John','+15550000000','Auto Accident','inbound_call')"),
                        {"i": lid, "o": org})
    return lid


async def test_send_sms_persists_message(firm, owner_engine):
    lead_id = await _make_lead(owner_engine, firm["org"])
    pid = await sms_service.send_sms(firm["org"], lead_id, _phone(), "hello", "follow_up")
    assert pid and pid.startswith("SM_fake_")
    async with owner_engine.begin() as c:
        row = (await c.execute(text("SELECT channel, direction, purpose, status, provider_message_id "
                                    "FROM messages WHERE lead_id=:l"), {"l": lead_id})).first()
        conv = (await c.execute(text("SELECT count(*) FROM conversations WHERE lead_id=:l AND channel='sms'"),
                                {"l": lead_id})).scalar_one()
    assert row.channel == "sms" and row.direction == "outbound" and row.purpose == "follow_up"
    assert row.status == "queued" and row.provider_message_id == pid
    assert conv == 1


async def test_voicemail_inbound_creates_fallback_lead(client, firm, owner_engine):
    sid = f"CA{uuid.uuid4().hex}"
    r = await client.post("/api/voice/inbound", data={"To": firm["dialed"], "From": _phone(), "CallSid": sid})
    assert r.status_code == 200 and "<Response>" in r.text
    async with owner_engine.begin() as c:
        lead = (await c.execute(
            text("SELECT id, pipeline_status FROM leads WHERE organization_id=:o ORDER BY created_at DESC LIMIT 1"),
            {"o": firm["org"]})).first()
        vc_lead = (await c.execute(text("SELECT lead_id FROM voice_calls WHERE provider_sid=:s"),
                                   {"s": sid})).scalar_one()
    assert lead.pipeline_status == "Needs Review"
    assert vc_lead == lead.id  # the call is linked to the fallback lead


async def test_dropped_call_sends_resume_sms(client, firm, owner_engine):
    """Call ends while the intake transcript is still in progress → resume SMS."""
    caller = _phone()
    sid = f"CA{uuid.uuid4().hex}"
    lead_id = await _make_lead(owner_engine, firm["org"])
    vc_id = uuid.uuid4()
    tr_id = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO voice_calls (id,organization_id,direction,from_e164,to_e164,"
                             "provider_sid,status,lead_id) VALUES (:i,:o,'inbound',:f,:t,:s,'in-progress',:l)"),
                        {"i": vc_id, "o": firm["org"], "f": caller, "t": firm["dialed"], "s": sid, "l": lead_id})
        await c.execute(text("INSERT INTO intake_transcripts (id,organization_id,lead_id,voice_call_id,status) "
                             "VALUES (:i,:o,:l,:v,'in_progress')"),
                        {"i": tr_id, "o": firm["org"], "l": lead_id, "v": vc_id})

    r = await client.post("/api/voice/status", data={
        "To": firm["dialed"], "CallSid": sid, "CallStatus": "completed", "CallDuration": "12",
    })
    assert r.status_code == 204
    async with owner_engine.begin() as c:
        msg = (await c.execute(text("SELECT body, purpose FROM messages WHERE lead_id=:l"),
                               {"l": lead_id})).first()
    assert msg is not None and msg.purpose == "follow_up"
    assert "ended early" in msg.body.lower()


async def test_recording_sends_callback_sms(client, firm, owner_engine):
    caller = _phone()
    sid = f"CA{uuid.uuid4().hex}"
    lead_id = await _make_lead(owner_engine, firm["org"])
    vc_id = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO voice_calls (id,organization_id,direction,from_e164,to_e164,"
                             "provider_sid,status,lead_id) VALUES (:i,:o,'inbound',:f,:t,:s,'completed',:l)"),
                        {"i": vc_id, "o": firm["org"], "f": caller, "t": firm["dialed"], "s": sid, "l": lead_id})

    r = await client.post("/api/voice/recording", data={
        "To": firm["dialed"], "From": caller, "CallSid": sid,
        "RecordingUrl": "https://api.twilio.com/rec/RE123",
    })
    assert r.status_code == 200
    async with owner_engine.begin() as c:
        msg = (await c.execute(text("SELECT purpose FROM messages WHERE lead_id=:l"),
                               {"l": lead_id})).first()
        rec = (await c.execute(text("SELECT recording_url FROM voice_calls WHERE id=:i"),
                               {"i": vc_id})).scalar_one()
    assert msg is not None and msg.purpose == "follow_up"
    assert rec == "https://api.twilio.com/rec/RE123"
