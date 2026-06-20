"""Phase 4 proof — AI-memory RLS + the end-to-end hybrid retrieval (DoD).

Seeded as the owner (RLS bypassed); asserted through the app engine (``app_user``).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import session_scope
from app.models.enums import EMBEDDING_DIM
from app.security.context import TenantContext


def staff_ctx(org, user, role):
    return TenantContext(
        organization_id=org, subject_type="user", subject_id=user, role=role
    )


def client_ctx(org, client_account_id):
    return TenantContext(
        organization_id=org, subject_type="client", subject_id=client_account_id
    )


def _vec(hot_index: int) -> str:
    """A unit vector with a single 1.0 at `hot_index` (distinct directions)."""
    return "[" + ",".join("1" if i == hot_index else "0" for i in range(EMBEDDING_DIM)) + "]"


def _rrf(rankings: list[list], k: int = 60) -> list:
    """Reciprocal Rank Fusion over several ranked id lists."""
    scores: dict = {}
    for ranking in rankings:
        for pos, _id in enumerate(ranking):
            scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + pos + 1)
    return sorted(scores, key=lambda x: scores[x], reverse=True)


async def _ids(session, sql: str) -> set:
    return set((await session.execute(text(sql))).scalars().all())


@pytest_asyncio.fixture
async def s4(owner_engine):
    d = {k: uuid.uuid4() for k in (
        "orgA", "orgB", "specA1", "specA2", "ownerB",
        "leadA1", "leadB", "clientA1",
        "kcA1", "kcA2", "kcB", "thA1", "aeA1", "oeA1",
    )}

    async with owner_engine.begin() as c:
        for org, slug in ((d["orgA"], "p4-a"), (d["orgB"], "p4-b")):
            await c.execute(
                text("INSERT INTO organizations (id, name, slug) VALUES (:id,:n,:s)"),
                {"id": org, "n": str(slug), "s": f"{slug}-{org.hex[:8]}"},
            )
        for uid, org, email, role in (
            (d["specA1"], d["orgA"], "s1@p4-a.test", "intake_specialist"),
            (d["specA2"], d["orgA"], "s2@p4-a.test", "intake_specialist"),
            (d["ownerB"], d["orgB"], "owner@p4-b.test", "owner"),
        ):
            await c.execute(
                text("INSERT INTO users (id, organization_id, email, role) VALUES (:id,:o,:e,:r)"),
                {"id": uid, "o": org, "e": email, "r": role},
            )
        for lid, org, assignee in (
            (d["leadA1"], d["orgA"], d["specA1"]),
            (d["leadB"], d["orgB"], d["ownerB"]),
        ):
            await c.execute(
                text("INSERT INTO leads (id, organization_id, full_name, phone, case_type, "
                     "assigned_user_id) VALUES (:id,:o,'L','+15550000000','Auto Accident',:a)"),
                {"id": lid, "o": org, "a": assignee},
            )
        await c.execute(
            text("INSERT INTO client_accounts (id, organization_id, lead_id, email) "
                 "VALUES (:id,:o,:l,'c@p4-a.test')"),
            {"id": d["clientA1"], "o": d["orgA"], "l": d["leadA1"]},
        )
        # Knowledge chunks: A1 is the relevant one (neck injury, vector dir 0).
        chunks = (
            (d["kcA1"], d["orgA"], d["leadA1"], "rear-end collision neck injury physical therapy", _vec(0)),
            (d["kcA2"], d["orgA"], d["leadA1"], "dog bite puncture wound stitches", _vec(1)),
            (d["kcB"], d["orgB"], d["leadB"], "rear-end collision neck injury physical therapy", _vec(0)),
        )
        for cid, org, lead, content, emb in chunks:
            await c.execute(
                text("INSERT INTO knowledge_chunks "
                     "(id, organization_id, lead_id, source_type, source_id, content, embedding) "
                     "VALUES (:id,:o,:l,'transcript',:src,:content, CAST(:emb AS halfvec))"),
                {"id": cid, "o": org, "l": lead, "src": uuid.uuid4(), "content": content, "emb": emb},
            )
        await c.execute(
            text("INSERT INTO agent_threads (id, organization_id, lead_id, thread_key) "
                 "VALUES (:id,:o,:l,:k)"),
            {"id": d["thA1"], "o": d["orgA"], "l": d["leadA1"], "k": f"t-{d['thA1'].hex}"},
        )
        await c.execute(
            text("INSERT INTO agent_events (id, organization_id, lead_id, event_type, name) "
                 "VALUES (:id,:o,:l,'tool_call','create_lead')"),
            {"id": d["aeA1"], "o": d["orgA"], "l": d["leadA1"]},
        )
        await c.execute(
            text("INSERT INTO outbox_events (id, organization_id, aggregate_type, event_type) "
                 "VALUES (:id,:o,'lead','lead.created')"),
            {"id": d["oeA1"], "o": d["orgA"]},
        )

    yield d

    async with owner_engine.begin() as c:
        await c.execute(
            text("DELETE FROM organizations WHERE id = ANY(:ids)"),
            {"ids": [d["orgA"], d["orgB"]]},
        )


async def test_hybrid_retrieval_scoped_to_lead(s4):
    """Vector + keyword search fused with RRF, scoped to one lead by RLS."""
    async with session_scope(staff_ctx(s4["orgA"], s4["specA1"], "intake_specialist")) as s:
        vector_ranked = [
            r for r in (await s.execute(
                text("SELECT id FROM knowledge_chunks "
                     "ORDER BY embedding <=> CAST(:q AS halfvec) LIMIT 10"),
                {"q": _vec(0)},
            )).scalars().all()
        ]
        keyword_ranked = [
            r for r in (await s.execute(
                text("SELECT id FROM knowledge_chunks "
                     "WHERE content_tsv @@ plainto_tsquery('english', :q) "
                     "ORDER BY ts_rank(content_tsv, plainto_tsquery('english', :q)) DESC LIMIT 10"),
                {"q": "neck injury"},
            )).scalars().all()
        ]

    fused = _rrf([vector_ranked, keyword_ranked])
    assert fused, "hybrid search returned nothing"
    assert fused[0] == s4["kcA1"]  # most relevant chunk ranks first
    # The other firm's chunk must never appear (RLS scoping).
    assert s4["kcB"] not in vector_ranked
    assert s4["kcB"] not in keyword_ranked


async def test_knowledge_chunks_cross_firm_isolation(s4):
    async with session_scope(staff_ctx(s4["orgB"], s4["ownerB"], "owner")) as s:
        seen = await _ids(s, "SELECT id FROM knowledge_chunks")
    assert s4["kcB"] in seen
    assert s4["kcA1"] not in seen


async def test_client_cannot_read_ai_memory(s4):
    async with session_scope(client_ctx(s4["orgA"], s4["clientA1"])) as s:
        chunks = await _ids(s, "SELECT id FROM knowledge_chunks")
        threads = await _ids(s, "SELECT id FROM agent_threads")
    assert chunks == set() and threads == set()


async def test_unassigned_staff_blocked_from_ai_memory(s4):
    async with session_scope(staff_ctx(s4["orgA"], s4["specA2"], "intake_specialist")) as s:
        chunks = await _ids(s, "SELECT id FROM knowledge_chunks")
    assert s4["kcA1"] not in chunks


async def test_agent_events_and_outbox_append_only(s4):
    ctx = staff_ctx(s4["orgA"], s4["specA1"], "intake_specialist")
    with pytest.raises(Exception):
        async with session_scope(ctx) as s:
            await s.execute(text("UPDATE agent_events SET name='x' WHERE lead_id=:l"), {"l": s4["leadA1"]})
    with pytest.raises(Exception):
        async with session_scope(ctx) as s:
            await s.execute(text("DELETE FROM outbox_events WHERE organization_id=:o"), {"o": s4["orgA"]})
