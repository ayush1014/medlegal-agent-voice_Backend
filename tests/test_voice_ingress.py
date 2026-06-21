"""Phase B — Twilio telephony ingress: org resolution, voice_calls, idempotency."""

from __future__ import annotations

import uuid

import httpx
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text

from app.config import settings
from app.database import session_scope
from app.main import app
from app.services import voice_service


def _phone() -> str:
    return "+1" + f"{uuid.uuid4().int % 10**10:010d}"


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(autouse=True)
def no_twilio_validation(monkeypatch):
    monkeypatch.setattr(settings, "twilio_validate_webhooks", False)
    monkeypatch.setattr(settings, "livekit_sip_uri", None)  # voicemail fallback by default


@pytest_asyncio.fixture
async def firm(owner_engine):
    org = uuid.uuid4()
    dialed = _phone()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": org, "s": f"voice-{org.hex[:8]}"})
        await c.execute(text("INSERT INTO phone_numbers (organization_id,e164,is_primary) "
                             "VALUES (:o,:e,true)"), {"o": org, "e": dialed})
    yield {"org": org, "dialed": dialed}
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": org})


async def _count_calls(owner_engine, sid) -> int:
    async with owner_engine.begin() as c:
        return (await c.execute(
            text("SELECT count(*) FROM voice_calls WHERE provider_sid=:s"), {"s": sid}
        )).scalar_one()


async def test_resolve_org_by_dialed_number(firm):
    async with session_scope(None) as db:
        org = await voice_service.resolve_org_by_dialed_number(db, firm["dialed"])
        missing = await voice_service.resolve_org_by_dialed_number(db, "+15550009999")
    assert org == firm["org"]
    assert missing is None


async def test_inbound_creates_voice_call_and_bridges(client, firm, owner_engine):
    sid = f"CA{uuid.uuid4().hex}"
    r = await client.post("/api/voice/inbound", data={
        "To": firm["dialed"], "From": _phone(), "CallSid": sid, "CallStatus": "ringing",
    })
    assert r.status_code == 200
    assert "<Response>" in r.text  # valid TwiML
    assert await _count_calls(owner_engine, sid) == 1

    async with owner_engine.begin() as c:
        row = (await c.execute(
            text("SELECT organization_id, direction, status FROM voice_calls WHERE provider_sid=:s"),
            {"s": sid},
        )).first()
    assert row.organization_id == firm["org"]
    assert row.direction == "inbound" and row.status == "ringing"


async def test_inbound_is_idempotent(client, firm, owner_engine):
    sid = f"CA{uuid.uuid4().hex}"
    payload = {"To": firm["dialed"], "From": _phone(), "CallSid": sid}
    await client.post("/api/voice/inbound", data=payload)
    await client.post("/api/voice/inbound", data=payload)  # Twilio retry
    assert await _count_calls(owner_engine, sid) == 1


async def test_inbound_bridges_to_sip_when_configured(client, firm, monkeypatch):
    monkeypatch.setattr(settings, "livekit_sip_uri", "demo.sip.livekit.cloud")
    sid = f"CA{uuid.uuid4().hex}"
    r = await client.post("/api/voice/inbound", data={"To": firm["dialed"], "From": _phone(), "CallSid": sid})
    assert "<Sip>" in r.text and "sip:" in r.text


async def test_status_finalizes_call(client, firm, owner_engine):
    sid = f"CA{uuid.uuid4().hex}"
    await client.post("/api/voice/inbound", data={"To": firm["dialed"], "From": _phone(), "CallSid": sid})
    r = await client.post("/api/voice/status", data={
        "To": firm["dialed"], "CallSid": sid, "CallStatus": "completed", "CallDuration": "42",
    })
    assert r.status_code == 204
    async with owner_engine.begin() as c:
        row = (await c.execute(
            text("SELECT status, duration_seconds, ended_at FROM voice_calls WHERE provider_sid=:s"),
            {"s": sid},
        )).first()
    assert row.status == "completed" and row.duration_seconds == 42 and row.ended_at is not None


async def test_unknown_number_not_in_service(client, owner_engine):
    sid = f"CA{uuid.uuid4().hex}"
    r = await client.post("/api/voice/inbound", data={"To": "+15550001111", "From": _phone(), "CallSid": sid})
    assert r.status_code == 200
    assert "not in service" in r.text and "<Hangup" in r.text
    assert await _count_calls(owner_engine, sid) == 0
