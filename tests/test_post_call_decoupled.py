"""Decoupled post-call pipeline: a `call.ended` outbox event (emitted fast by the
voice worker on hangup) is drained server-side into full extraction + scoring,
with no dependency on the worker staying alive. Live (OpenAI extraction/embeddings).
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy import text

from app.agent.context import IntakeContext
from app.database import session_scope
from app.jobs.post_call import process_pending_call_ended
from app.security.context import system_context
from app.services import outbox_service


@pytest_asyncio.fixture
async def org(owner_engine):
    oid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": oid, "s": f"pc-{oid.hex[:8]}"})
    yield oid
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": oid})


async def test_call_ended_drains_into_extraction_and_scoring(org, owner_engine):
    lead_id = uuid.uuid4()
    transcript_id = uuid.uuid4()
    transcript = (
        "Agent: Thanks for calling, what happened?\n"
        "Caller: My name is Maria Lopez. I was rear-ended at a red light on June 10th 2026.\n"
        "Caller: My neck and lower back hurt a lot — it's pretty severe.\n"
        "Caller: I went to the ER and then started physical therapy at City Rehab.\n"
        "Caller: The other driver ran the light. I have State Farm insurance.\n"
        "Agent: Thank you, we'll be in touch."
    )
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO leads (id,organization_id,full_name,phone,case_type,source,"
                             "pipeline_status) VALUES (:i,:o,'Caller','+15557770000','Other Personal Injury',"
                             "'inbound_call','Intake Started')"), {"i": lead_id, "o": org})
        await c.execute(text("INSERT INTO intake_transcripts (id,organization_id,lead_id,status,language,"
                             "full_text) VALUES (:t,:o,:l,'complete','en',:ft)"),
                        {"t": transcript_id, "o": org, "l": lead_id, "ft": transcript})

    # Worker would emit this fast event on hangup (caller_phone=None → no real SMS in test).
    async with session_scope(system_context(org)) as db:
        await outbox_service.emit_event(
            db, org, aggregate_type="lead", aggregate_id=lead_id, event_type="call.ended",
            payload={"transcript_id": str(transcript_id), "voice_call_id": None, "caller_phone": None})

    res = await process_pending_call_ended()
    assert res["processed"] == 1 and res["failed"] == 0

    async with owner_engine.begin() as c:
        ev_status = (await c.execute(text("SELECT status FROM outbox_events WHERE event_type='call.ended' "
                                          "AND aggregate_id=:l"), {"l": lead_id})).scalar_one()
        n_inj = (await c.execute(text("SELECT count(*) FROM injuries WHERE lead_id=:l"), {"l": lead_id})).scalar_one()
        n_score = (await c.execute(text("SELECT count(*) FROM lead_scores WHERE lead_id=:l"), {"l": lead_id})).scalar_one()
        pipeline = (await c.execute(text("SELECT pipeline_status FROM leads WHERE id=:l"), {"l": lead_id})).scalar_one()

    assert ev_status == "published"          # event drained
    assert n_inj >= 1                        # extraction ran + persisted facts
    assert n_score >= 1                      # intelligence (scoring) ran
    assert pipeline != "Intake Started"      # funnel advanced the lead


async def test_call_ended_missing_transcript_retries(org, owner_engine):
    lead_id = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO leads (id,organization_id,full_name,phone,case_type,source) "
                             "VALUES (:i,:o,'Caller','+15557770001','Other Personal Injury','inbound_call')"),
                        {"i": lead_id, "o": org})
    async with session_scope(system_context(org)) as db:
        await outbox_service.emit_event(
            db, org, aggregate_type="lead", aggregate_id=lead_id, event_type="call.ended",
            payload={"transcript_id": str(uuid.uuid4()), "caller_phone": None})  # transcript doesn't exist

    res = await process_pending_call_ended()
    assert res["processed"] == 0  # missing transcript → never counted as processed
    async with owner_engine.begin() as c:
        row = (await c.execute(text("SELECT status, attempts FROM outbox_events WHERE aggregate_id=:l"),
                               {"l": lead_id})).first()
    # Not lost: kept for retry (pending), or parked failed after enough attempts. attempts>=1.
    # (Exact count is non-deterministic when a live worker shares this DB.)
    assert row.status in ("pending", "failed") and row.attempts >= 1
