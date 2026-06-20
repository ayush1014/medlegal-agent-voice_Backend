"""Phase 3 RLS proof — comms, documents/retainer, workflow/audit.

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
async def s3(owner_engine):
    d = {k: uuid.uuid4() for k in (
        "orgA", "orgB", "ownerA", "specA1", "specA2", "ownerB",
        "leadA1", "leadB", "clientA1",
        "convA1", "msgA1", "drA1", "docA1", "retA1", "sigA1", "noteA1",
        "taskA1", "taskNull", "vcNull", "auditA1",
    )}

    async with owner_engine.begin() as c:
        for org, slug in ((d["orgA"], "p3-a"), (d["orgB"], "p3-b")):
            await c.execute(
                text("INSERT INTO organizations (id, name, slug) VALUES (:id,:n,:s)"),
                {"id": org, "n": str(slug), "s": f"{slug}-{org.hex[:8]}"},
            )
        for uid, org, email, role in (
            (d["ownerA"], d["orgA"], "owner@p3-a.test", "owner"),
            (d["specA1"], d["orgA"], "s1@p3-a.test", "intake_specialist"),
            (d["specA2"], d["orgA"], "s2@p3-a.test", "intake_specialist"),
            (d["ownerB"], d["orgB"], "owner@p3-b.test", "owner"),
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
                text(
                    "INSERT INTO leads (id, organization_id, full_name, phone, case_type, "
                    "assigned_user_id) VALUES (:id,:o,'L','+15550000000','Auto Accident',:a)"
                ),
                {"id": lid, "o": org, "a": assignee},
            )
        await c.execute(
            text("INSERT INTO client_accounts (id, organization_id, lead_id, email) "
                 "VALUES (:id,:o,:l,'c@p3-a.test')"),
            {"id": d["clientA1"], "o": d["orgA"], "l": d["leadA1"]},
        )
        await c.execute(
            text("INSERT INTO conversations (id, organization_id, lead_id, channel) "
                 "VALUES (:id,:o,:l,'sms')"),
            {"id": d["convA1"], "o": d["orgA"], "l": d["leadA1"]},
        )
        await c.execute(
            text("INSERT INTO messages (id, organization_id, conversation_id, lead_id, "
                 "channel, direction) VALUES (:id,:o,:cv,:l,'sms','outbound')"),
            {"id": d["msgA1"], "o": d["orgA"], "cv": d["convA1"], "l": d["leadA1"]},
        )
        await c.execute(
            text("INSERT INTO document_requests (id, organization_id, lead_id, document_type) "
                 "VALUES (:id,:o,:l,'Police report')"),
            {"id": d["drA1"], "o": d["orgA"], "l": d["leadA1"]},
        )
        await c.execute(
            text("INSERT INTO documents (id, organization_id, lead_id, file_name) "
                 "VALUES (:id,:o,:l,'report.pdf')"),
            {"id": d["docA1"], "o": d["orgA"], "l": d["leadA1"]},
        )
        await c.execute(
            text("INSERT INTO retainers (id, organization_id, lead_id, status) "
                 "VALUES (:id,:o,:l,'Sent')"),
            {"id": d["retA1"], "o": d["orgA"], "l": d["leadA1"]},
        )
        await c.execute(
            text("INSERT INTO signature_events (id, organization_id, retainer_id, lead_id, event) "
                 "VALUES (:id,:o,:r,:l,'sent')"),
            {"id": d["sigA1"], "o": d["orgA"], "r": d["retA1"], "l": d["leadA1"]},
        )
        await c.execute(
            text("INSERT INTO internal_notes (id, organization_id, lead_id, author_user_id, body) "
                 "VALUES (:id,:o,:l,:a,'note')"),
            {"id": d["noteA1"], "o": d["orgA"], "l": d["leadA1"], "a": d["ownerA"]},
        )
        await c.execute(
            text("INSERT INTO tasks (id, organization_id, lead_id, title) VALUES (:id,:o,:l,'t1')"),
            {"id": d["taskA1"], "o": d["orgA"], "l": d["leadA1"]},
        )
        await c.execute(
            text("INSERT INTO tasks (id, organization_id, title) VALUES (:id,:o,'firm-wide')"),
            {"id": d["taskNull"], "o": d["orgA"]},
        )
        await c.execute(
            text("INSERT INTO voice_calls (id, organization_id, direction) VALUES (:id,:o,'inbound')"),
            {"id": d["vcNull"], "o": d["orgA"]},
        )
        await c.execute(
            text("INSERT INTO audit_logs (id, organization_id, actor_type, action) "
                 "VALUES (:id,:o,'system','seed')"),
            {"id": d["auditA1"], "o": d["orgA"]},
        )

    yield d

    async with owner_engine.begin() as c:
        await c.execute(
            text("DELETE FROM organizations WHERE id = ANY(:ids)"),
            {"ids": [d["orgA"], d["orgB"]]},
        )


async def test_assigned_staff_sees_comms(s3):
    async with session_scope(staff_ctx(s3["orgA"], s3["specA1"], "intake_specialist")) as s:
        convs = await _ids(s, "SELECT id FROM conversations")
        msgs = await _ids(s, "SELECT id FROM messages")
    assert s3["convA1"] in convs
    assert s3["msgA1"] in msgs


async def test_unassigned_staff_blocked_from_comms(s3):
    async with session_scope(staff_ctx(s3["orgA"], s3["specA2"], "intake_specialist")) as s:
        msgs = await _ids(s, "SELECT id FROM messages")
        docs = await _ids(s, "SELECT id FROM documents")
    assert s3["msgA1"] not in msgs
    assert s3["docA1"] not in docs


async def test_other_firm_blocked(s3):
    async with session_scope(staff_ctx(s3["orgB"], s3["ownerB"], "owner")) as s:
        docs = await _ids(s, "SELECT id FROM documents")
        rets = await _ids(s, "SELECT id FROM retainers")
    assert s3["docA1"] not in docs
    assert s3["retA1"] not in rets


async def test_client_cannot_read_docs_or_retainer(s3):
    async with session_scope(client_ctx(s3["orgA"], s3["clientA1"])) as s:
        docs = await _ids(s, "SELECT id FROM documents")
        reqs = await _ids(s, "SELECT id FROM document_requests")
        rets = await _ids(s, "SELECT id FROM retainers")
    assert docs == set() and reqs == set() and rets == set()


async def test_nullable_lead_rows_visible_firm_wide(s3):
    # A firm-wide task/call (lead_id NULL) is visible to any staff in the firm.
    async with session_scope(staff_ctx(s3["orgA"], s3["specA2"], "intake_specialist")) as s:
        tasks = await _ids(s, "SELECT id FROM tasks")
        calls = await _ids(s, "SELECT id FROM voice_calls")
    assert s3["taskNull"] in tasks
    assert s3["vcNull"] in calls
    assert s3["taskA1"] not in tasks  # tied to a lead specA2 can't access


async def test_audit_logs_readable_only_by_admins(s3):
    async with session_scope(staff_ctx(s3["orgA"], s3["ownerA"], "owner")) as s:
        seen_by_owner = await _ids(s, "SELECT id FROM audit_logs")
    async with session_scope(staff_ctx(s3["orgA"], s3["specA1"], "intake_specialist")) as s:
        seen_by_staff = await _ids(s, "SELECT id FROM audit_logs")
    assert s3["auditA1"] in seen_by_owner
    assert s3["auditA1"] not in seen_by_staff


async def test_append_only_signature_events_and_audit(s3):
    owner = staff_ctx(s3["orgA"], s3["ownerA"], "owner")
    with pytest.raises(Exception):
        async with session_scope(owner) as s:
            await s.execute(text("UPDATE signature_events SET event='viewed' WHERE lead_id=:l"), {"l": s3["leadA1"]})
    with pytest.raises(Exception):
        async with session_scope(system_ctx(s3["orgA"])) as s:
            await s.execute(text("DELETE FROM audit_logs WHERE organization_id=:o"), {"o": s3["orgA"]})


async def test_system_can_append_audit(s3):
    async with session_scope(system_ctx(s3["orgA"])) as s:
        await s.execute(
            text("INSERT INTO audit_logs (organization_id, actor_type, action) "
                 "VALUES (:o,'system','did_thing')"),
            {"o": s3["orgA"]},
        )
        count = (await s.execute(
            text("SELECT count(*) FROM audit_logs WHERE organization_id=:o"), {"o": s3["orgA"]}
        )).scalar_one()
    assert count >= 2
