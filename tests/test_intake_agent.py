"""Phase C — intake agent: safety detection, tools, persistence, and a live
DeepSeek conversation smoke."""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy import text

from app.agent.context import IntakeContext
from app.agent.intake_agent import IntakeSession
from app.agent.safety import (
    detect_already_represented,
    detect_emergency,
    detect_language_choice,
)
from app.agent.tools import build_tools
from app.database import session_scope
from app.security.context import system_context
from app.services import intake_service


def _phone() -> str:
    return "+1" + f"{uuid.uuid4().int % 10**10:010d}"


@pytest_asyncio.fixture
async def org(owner_engine):
    oid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": oid, "s": f"intake-{oid.hex[:8]}"})
    yield oid
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": oid})


def _ctx(org) -> IntakeContext:
    return IntakeContext(organization_id=org, caller_phone=_phone(), firm_name="medLegal")


# --- Deterministic pieces (no LLM) ---

def test_safety_detection():
    assert detect_emergency("I think I can't breathe") is True
    assert detect_emergency("my lower back is sore") is False
    assert detect_already_represented("I already have a lawyer for this") is True
    assert detect_already_represented("no lawyer yet") is False
    assert detect_language_choice("español por favor") == "es"
    assert detect_language_choice("english is fine") == "en"


async def test_create_session_records(org, owner_engine):
    ctx = _ctx(org)
    async with session_scope(system_context(org)) as db:
        await intake_service.create_session_records(db, ctx)
    assert ctx.lead_id and ctx.transcript_id and ctx.agent_thread_id
    async with owner_engine.begin() as c:
        lead = (await c.execute(text("SELECT source, pipeline_status FROM leads WHERE id=:i"),
                                {"i": ctx.lead_id})).first()
        tr = (await c.execute(text("SELECT status, language FROM intake_transcripts WHERE id=:i"),
                              {"i": ctx.transcript_id})).first()
    assert lead.source == "inbound_call" and lead.pipeline_status == "Intake Started"
    assert tr.status == "in_progress" and tr.language == "en"


async def test_save_partial_lead_tool_normalizes_case_type(org, owner_engine):
    ctx = _ctx(org)
    async with session_scope(system_context(org)) as db:
        await intake_service.create_session_records(db, ctx)
    tools = {t.name: t for t in build_tools(ctx)}
    await tools["save_partial_lead"].ainvoke({"full_name": "Jane Roe", "case_type": "car crash"})
    async with owner_engine.begin() as c:
        lead = (await c.execute(text("SELECT full_name, case_type FROM leads WHERE id=:i"),
                                {"i": ctx.lead_id})).first()
        events = (await c.execute(
            text("SELECT count(*) FROM agent_events WHERE lead_id=:i AND name='save_partial_lead'"),
            {"i": ctx.lead_id})).scalar_one()
    assert lead.full_name == "Jane Roe"
    assert lead.case_type == "Other Personal Injury"  # invalid input normalized
    assert events == 1


async def test_flag_emergency_tool(org, owner_engine):
    ctx = _ctx(org)
    async with session_scope(system_context(org)) as db:
        await intake_service.create_session_records(db, ctx)
    tools = {t.name: t for t in build_tools(ctx)}
    await tools["flag_emergency"].ainvoke({})
    assert ctx.emergency is True
    async with owner_engine.begin() as c:
        n = (await c.execute(
            text("SELECT count(*) FROM agent_events WHERE lead_id=:i AND name='flag_emergency'"),
            {"i": ctx.lead_id})).scalar_one()
    assert n == 1


async def test_start_and_emergency_path_no_llm(org, owner_engine):
    ctx = _ctx(org)
    session = IntakeSession(ctx)  # model built but not used on these paths
    greeting = await session.start()
    assert "recorded" in greeting.lower() and "medLegal" in greeting

    reply = await session.respond("I think I'm having a heart attack")
    assert "911" in reply
    assert ctx.emergency is True and ctx.ended is True

    async with owner_engine.begin() as c:
        segs = (await c.execute(
            text("SELECT speaker FROM transcript_segments WHERE lead_id=:i ORDER BY seq"),
            {"i": ctx.lead_id})).scalars().all()
    # greeting (agent) + caller turn + emergency reply (agent)
    assert segs == ["agent", "caller", "agent"]


# --- Live DeepSeek conversation smoke (real model) ---

async def test_intake_llm_smoke(org, owner_engine):
    ctx = _ctx(org)
    session = IntakeSession(ctx)
    await session.start()
    r1 = await session.respond("English is fine")
    r2 = await session.respond(
        "My name is John Doe. I was rear-ended at a red light last week and my neck hurts."
    )
    await session.finalize()

    assert isinstance(r1, str) and r1.strip()
    assert isinstance(r2, str) and r2.strip()
    async with owner_engine.begin() as c:
        seg_count = (await c.execute(
            text("SELECT count(*) FROM transcript_segments WHERE lead_id=:i"),
            {"i": ctx.lead_id})).scalar_one()
        tr = (await c.execute(text("SELECT status FROM intake_transcripts WHERE id=:i"),
                              {"i": ctx.transcript_id})).first()
    assert seg_count >= 4  # greeting + 2 caller turns + 2 agent replies
    assert tr.status == "complete"
