"""Phase 2 RLS proof — lead visibility, client portal scope, append-only outputs.

Seeded as the owner (RLS bypassed); asserted through the app engine (``app_user``).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import session_scope
from app.security.context import TenantContext


def staff_ctx(org, user, role):
    return TenantContext(
        organization_id=org, subject_type="user", subject_id=user, role=role
    )


def client_ctx(org, client_account_id):
    return TenantContext(
        organization_id=org, subject_type="client", subject_id=client_account_id
    )


def system_ctx(org):
    return TenantContext(organization_id=org, subject_type="system")


async def _ids(session, sql: str) -> set:
    return set((await session.execute(text(sql))).scalars().all())


@pytest_asyncio.fixture
async def s2(owner_engine):
    """Two firms; firm A has an owner + two intake specialists, a lead assigned to
    each specialist, a client account on lead A1, and child/AI rows on lead A1."""
    d = {k: uuid.uuid4() for k in (
        "orgA", "orgB", "ownerA", "specA1", "specA2", "ownerB",
        "leadA1", "leadA2", "leadB", "clientA1", "injuryA1", "scoreA1", "estA1",
    )}

    async with owner_engine.begin() as c:
        for org, slug in ((d["orgA"], "p2-a"), (d["orgB"], "p2-b")):
            await c.execute(
                text("INSERT INTO organizations (id, name, slug) VALUES (:id,:n,:s)"),
                {"id": org, "n": str(slug), "s": f"{slug}-{org.hex[:8]}"},
            )
        for uid, org, email, role in (
            (d["ownerA"], d["orgA"], "owner@p2-a.test", "owner"),
            (d["specA1"], d["orgA"], "s1@p2-a.test", "intake_specialist"),
            (d["specA2"], d["orgA"], "s2@p2-a.test", "intake_specialist"),
            (d["ownerB"], d["orgB"], "owner@p2-b.test", "owner"),
        ):
            await c.execute(
                text(
                    "INSERT INTO users (id, organization_id, email, role) "
                    "VALUES (:id,:o,:e,:r)"
                ),
                {"id": uid, "o": org, "e": email, "r": role},
            )
        for lid, org, assignee in (
            (d["leadA1"], d["orgA"], d["specA1"]),
            (d["leadA2"], d["orgA"], d["specA2"]),
            (d["leadB"], d["orgB"], d["ownerB"]),
        ):
            await c.execute(
                text(
                    "INSERT INTO leads (id, organization_id, full_name, phone, "
                    "case_type, assigned_user_id) "
                    "VALUES (:id,:o,'Test Lead','+15550000000','Auto Accident',:a)"
                ),
                {"id": lid, "o": org, "a": assignee},
            )
        await c.execute(
            text(
                "INSERT INTO client_accounts (id, organization_id, lead_id, email) "
                "VALUES (:id,:o,:l,'client@p2-a.test')"
            ),
            {"id": d["clientA1"], "o": d["orgA"], "l": d["leadA1"]},
        )
        await c.execute(
            text(
                "INSERT INTO injuries (id, organization_id, lead_id, severity) "
                "VALUES (:id,:o,:l,'Minor')"
            ),
            {"id": d["injuryA1"], "o": d["orgA"], "l": d["leadA1"]},
        )
        await c.execute(
            text(
                "INSERT INTO lead_scores (id, organization_id, lead_id, score) "
                "VALUES (:id,:o,:l,77)"
            ),
            {"id": d["scoreA1"], "o": d["orgA"], "l": d["leadA1"]},
        )
        await c.execute(
            text(
                "INSERT INTO settlement_estimates (id, organization_id, lead_id, expected) "
                "VALUES (:id,:o,:l,25000)"
            ),
            {"id": d["estA1"], "o": d["orgA"], "l": d["leadA1"]},
        )

    yield d

    async with owner_engine.begin() as c:
        await c.execute(
            text("DELETE FROM organizations WHERE id = ANY(:ids)"),
            {"ids": [d["orgA"], d["orgB"]]},
        )


async def test_staff_sees_only_assigned_lead(s2):
    async with session_scope(staff_ctx(s2["orgA"], s2["specA1"], "intake_specialist")) as s:
        leads = await _ids(s, "SELECT id FROM leads")
    assert s2["leadA1"] in leads
    assert s2["leadA2"] not in leads  # assigned to the other specialist


async def test_owner_has_god_view_within_firm(s2):
    async with session_scope(staff_ctx(s2["orgA"], s2["ownerA"], "owner")) as s:
        leads = await _ids(s, "SELECT id FROM leads")
    assert {s2["leadA1"], s2["leadA2"]}.issubset(leads)
    assert s2["leadB"] not in leads  # other firm


async def test_client_sees_only_their_lead(s2):
    async with session_scope(client_ctx(s2["orgA"], s2["clientA1"])) as s:
        leads = await _ids(s, "SELECT id FROM leads")
    assert leads == {s2["leadA1"]}


async def test_client_cannot_read_child_tables(s2):
    async with session_scope(client_ctx(s2["orgA"], s2["clientA1"])) as s:
        injuries = await _ids(s, "SELECT id FROM injuries")
        scores = await _ids(s, "SELECT id FROM lead_scores")
    assert injuries == set()  # child fact tables are staff/system only
    assert scores == set()


async def test_assigned_staff_reads_child_rows(s2):
    async with session_scope(staff_ctx(s2["orgA"], s2["specA1"], "intake_specialist")) as s:
        injuries = await _ids(s, "SELECT id FROM injuries")
    assert s2["injuryA1"] in injuries


async def test_unassigned_staff_cannot_read_child_rows(s2):
    async with session_scope(staff_ctx(s2["orgA"], s2["specA2"], "intake_specialist")) as s:
        injuries = await _ids(s, "SELECT id FROM injuries")
    assert s2["injuryA1"] not in injuries  # specA2 isn't assigned lead A1


async def test_system_can_insert_child_rows(s2):
    async with session_scope(system_ctx(s2["orgA"])) as s:
        await s.execute(
            text(
                "INSERT INTO injuries (organization_id, lead_id, severity) "
                "VALUES (:o,:l,'Severe')"
            ),
            {"o": s2["orgA"], "l": s2["leadA1"]},
        )
        count = (
            await s.execute(text("SELECT count(*) FROM injuries WHERE lead_id = :l"), {"l": s2["leadA1"]})
        ).scalar_one()
    assert count >= 2


async def test_append_only_blocks_update_and_delete(s2):
    ctx = staff_ctx(s2["orgA"], s2["ownerA"], "owner")
    with pytest.raises(Exception):
        async with session_scope(ctx) as s:
            await s.execute(text("UPDATE lead_scores SET score = 1 WHERE lead_id = :l"), {"l": s2["leadA1"]})
    with pytest.raises(Exception):
        async with session_scope(ctx) as s:
            await s.execute(text("DELETE FROM settlement_estimates WHERE lead_id = :l"), {"l": s2["leadA1"]})


async def test_append_only_allows_insert(s2):
    async with session_scope(staff_ctx(s2["orgA"], s2["ownerA"], "owner")) as s:
        await s.execute(
            text(
                "INSERT INTO lead_scores (organization_id, lead_id, score) "
                "VALUES (:o,:l,90)"
            ),
            {"o": s2["orgA"], "l": s2["leadA1"]},
        )
        count = (
            await s.execute(text("SELECT count(*) FROM lead_scores WHERE lead_id = :l"), {"l": s2["leadA1"]})
        ).scalar_one()
    assert count >= 2
