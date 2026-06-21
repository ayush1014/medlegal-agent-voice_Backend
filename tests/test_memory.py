"""Phase F — case memory: knowledge graph (deterministic) + live RAG embeddings."""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy import text

from app.agent.context import IntakeContext
from app.agent.embeddings import embed_text
from app.agent.extraction import (
    Extraction,
    ExtractedIncident,
    ExtractedInjury,
    ExtractedLead,
    ExtractedParty,
    ExtractedPolicy,
    ExtractedTreatment,
)
from app.database import session_scope
from app.security.context import system_context
from app.services import memory_service

SAMPLE = """\
Caller: My name is John Doe.
Caller: I was rear-ended at a red light and my neck and lower back hurt a lot.
Caller: I went to urgent care and I'm starting physical therapy.
Caller: The other driver has Geico, I have State Farm.
"""


async def _make_lead(owner_engine, org) -> uuid.UUID:
    lead_id = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(
            text("INSERT INTO leads (id, organization_id, full_name, phone, case_type, source) "
                 "VALUES (:id,:o,'John Doe','+15550000000','Auto Accident','inbound_call')"),
            {"id": lead_id, "o": org},
        )
    return lead_id


@pytest_asyncio.fixture
async def org(owner_engine):
    oid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": oid, "s": f"mem-{oid.hex[:8]}"})
    yield oid
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": oid})


def test_chunk_transcript():
    chunks = memory_service.chunk_transcript(SAMPLE, max_chars=80)
    assert len(chunks) >= 2
    assert all(c.strip() for c in chunks)


async def test_build_case_graph(org, owner_engine):
    lead_id = await _make_lead(owner_engine, org)
    ctx = IntakeContext(organization_id=org, caller_phone="", lead_id=lead_id)
    ex = Extraction(
        lead=ExtractedLead(full_name="John Doe"),
        incidents=[ExtractedIncident(description="rear-ended at a light")],
        injuries=[ExtractedInjury(body_part="neck"), ExtractedInjury(body_part="back")],
        treatments=[ExtractedTreatment(provider_name="Urgent Care")],
        parties=[ExtractedParty(role="at_fault", full_name="Other Driver")],
        insurance_policies=[
            ExtractedPolicy(party_role="claimant", carrier_name="State Farm"),
            ExtractedPolicy(party_role="at_fault", carrier_name="Geico"),
        ],
    )
    async with session_scope(system_context(org)) as db:
        counts = await memory_service.build_case_graph(db, ctx, ex)

    # client + incident + 2 injuries + provider + at_fault person + 2 insurers = 8 nodes
    assert counts == {"nodes": 8, "edges": 7}
    async with owner_engine.begin() as c:
        rels = (await c.execute(text("SELECT relation FROM kg_edges WHERE lead_id=:l"),
                                {"l": lead_id})).scalars().all()
    for r in ("injured_in", "suffered", "treated_by", "insured_by"):
        assert r in rels


# --- Live OpenAI embeddings + hybrid retrieval ---

async def test_persist_memory_and_hybrid_search(org, owner_engine):
    lead_id = await _make_lead(owner_engine, org)
    counts = await memory_service.persist_memory(org, lead_id, uuid.uuid4(), SAMPLE, Extraction())
    assert counts["chunks"] >= 1 and counts["nodes"] >= 1

    async with owner_engine.begin() as c:
        n = (await c.execute(text("SELECT count(*) FROM knowledge_chunks WHERE lead_id=:l"),
                             {"l": lead_id})).scalar_one()
    assert n == counts["chunks"]

    qvec = await embed_text("neck and back pain")
    ctx = IntakeContext(organization_id=org, caller_phone="", lead_id=lead_id)
    async with session_scope(system_context(org)) as db:
        results = await memory_service.hybrid_search(db, ctx, qvec, "neck and back pain", k=3)
    assert results
    joined = " ".join(r["content"] for r in results).lower()
    assert "neck" in joined or "back" in joined


async def test_memory_isolated_per_lead(org, owner_engine):
    lead_a = await _make_lead(owner_engine, org)
    lead_b = await _make_lead(owner_engine, org)
    await memory_service.persist_memory(org, lead_a, uuid.uuid4(), SAMPLE, Extraction())
    await memory_service.persist_memory(
        org, lead_b, uuid.uuid4(), "Caller: I was bitten by a dog and needed stitches.", Extraction()
    )

    qvec = await embed_text("dog bite stitches")
    ctx_a = IntakeContext(organization_id=org, caller_phone="", lead_id=lead_a)
    async with session_scope(system_context(org)) as db:
        results = await memory_service.hybrid_search(db, ctx_a, qvec, "dog bite stitches", k=5)
    # Searching lead A must never surface lead B's dog-bite chunk.
    joined = " ".join(r["content"] for r in results).lower()
    assert "dog" not in joined
