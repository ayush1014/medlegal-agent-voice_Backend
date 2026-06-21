"""Phase H — handoff: outbox event, cost capture, welcome SMS, full post-call pipeline."""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy import text

from app.agent.context import IntakeContext
from app.database import session_scope
from app.security.context import system_context
from app.services import cost_service, intake_pipeline, outbox_service, sms_service

SAMPLE = """\
Agent: Thank you for calling medLegal. This call is recorded.
Caller: My name is John Doe.
Caller: I was rear-ended at a red light last week and my neck and back hurt.
Caller: I went to urgent care and I'm starting physical therapy.
Caller: I have State Farm; the other driver has Geico. I don't have a lawyer yet.
"""


def _phone() -> str:
    return "+1" + f"{uuid.uuid4().int % 10**10:010d}"


@pytest_asyncio.fixture(autouse=True)
def mock_twilio(monkeypatch):
    monkeypatch.setattr(sms_service, "_twilio_create_message",
                        lambda f, t, b: ("SM_" + uuid.uuid4().hex[:8], "queued"))


@pytest_asyncio.fixture
async def firm(owner_engine):
    org = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": org, "s": f"ho-{org.hex[:8]}"})
        await c.execute(text("INSERT INTO phone_numbers (organization_id,e164,is_primary) "
                             "VALUES (:o,:e,true)"), {"o": org, "e": _phone()})
    yield org
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": org})


async def _make_lead(owner_engine, org) -> uuid.UUID:
    lid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO leads (id,organization_id,full_name,phone,case_type,source,"
                             "pipeline_status) VALUES (:i,:o,'Caller','+15550000000',"
                             "'Other Personal Injury','inbound_call','Intake Started')"),
                        {"i": lid, "o": org})
    return lid


def test_cost_estimate():
    c = cost_service.estimate_call_cost(120, 600)
    assert c["voice_minutes"] == 2.0
    assert c["total_usd"] > 0 and c["estimate"] is True


async def test_emit_outbox_event(firm, owner_engine):
    agg = uuid.uuid4()
    async with session_scope(system_context(firm)) as db:
        eid = await outbox_service.emit_event(
            db, firm, aggregate_type="lead", aggregate_id=agg,
            event_type="intake.completed", payload={"lead_id": str(agg)},
        )
    async with owner_engine.begin() as c:
        row = (await c.execute(text("SELECT event_type, status, aggregate_type FROM outbox_events WHERE id=:i"),
                               {"i": eid})).first()
    assert row.event_type == "intake.completed" and row.status == "pending" and row.aggregate_type == "lead"


async def test_full_post_call_pipeline(firm, owner_engine):
    lead_id = await _make_lead(owner_engine, firm)
    result = await intake_pipeline.run_post_call_pipeline(
        organization_id=firm,
        lead_id=lead_id,
        transcript_text=SAMPLE,
        transcript_id=uuid.uuid4(),
        voice_call_id=uuid.uuid4(),
        caller_phone=_phone(),
        duration_seconds=95,
    )
    assert result["extraction"]["injuries"] >= 1
    assert result["chunks"] >= 1

    async with owner_engine.begin() as c:
        injuries = (await c.execute(text("SELECT count(*) FROM injuries WHERE lead_id=:l"), {"l": lead_id})).scalar_one()
        chunks = (await c.execute(text("SELECT count(*) FROM knowledge_chunks WHERE lead_id=:l"), {"l": lead_id})).scalar_one()
        nodes = (await c.execute(text("SELECT count(*) FROM kg_nodes WHERE lead_id=:l"), {"l": lead_id})).scalar_one()
        outbox = (await c.execute(text("SELECT event_type, status FROM outbox_events WHERE aggregate_id=:l"), {"l": lead_id})).first()
        usage = (await c.execute(text("SELECT count(*) FROM agent_events WHERE lead_id=:l AND name='call_usage'"), {"l": lead_id})).scalar_one()
        welcome = (await c.execute(text("SELECT purpose FROM messages WHERE lead_id=:l"), {"l": lead_id})).first()
        pipeline = (await c.execute(text("SELECT pipeline_status FROM leads WHERE id=:l"), {"l": lead_id})).scalar_one()

    assert injuries >= 1 and chunks >= 1 and nodes >= 1
    # The funnel now dispatches intake.completed -> lead intelligence runs.
    assert outbox is not None and outbox.event_type == "intake.completed"
    assert outbox.status in ("published", "pending")
    assert usage == 1
    assert welcome is not None and welcome.purpose == "general"
    # Intelligence advances the lead past Intake Complete into a scored stage.
    assert pipeline in ("Qualified", "Needs Review", "Rejected", "Intake Complete")
