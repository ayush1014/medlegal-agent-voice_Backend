"""RLS isolation proof (PRD Definition of Done).

All queries run through the app engine (``app_user``, NOBYPASSRLS). A query under
firm A must never see firm B's rows, and an unset context must see nothing.
"""

from __future__ import annotations

from sqlalchemy import text

from app.database import session_scope
from app.security.context import TenantContext


def _staff_ctx(org, user):
    return TenantContext(
        organization_id=org, subject_type="user", subject_id=user, role="owner"
    )


async def _ids(session, sql: str) -> set:
    return set((await session.execute(text(sql))).scalars().all())


async def test_org_sees_only_itself(seed):
    async with session_scope(_staff_ctx(seed["orgA"], seed["userA"])) as s:
        orgs = await _ids(s, "SELECT id FROM organizations")
    assert seed["orgA"] in orgs
    assert seed["orgB"] not in orgs


async def test_users_isolated_across_firms(seed):
    async with session_scope(_staff_ctx(seed["orgA"], seed["userA"])) as s:
        users = await _ids(s, "SELECT id FROM users")
    assert seed["userA"] in users
    assert seed["userB"] not in users


async def test_phone_numbers_isolated_across_firms(seed):
    async with session_scope(_staff_ctx(seed["orgB"], seed["userB"])) as s:
        phones = await _ids(s, "SELECT id FROM phone_numbers")
    assert seed["phoneB"] in phones
    assert seed["phoneA"] not in phones


async def test_sessions_scoped_to_subject(seed):
    # Same firm, but a different subject must not see another subject's session.
    async with session_scope(_staff_ctx(seed["orgA"], seed["userA"])) as s:
        own = await _ids(s, "SELECT id FROM user_sessions")
    assert seed["sessionA"] in own
    assert seed["sessionB"] not in own

    async with session_scope(_staff_ctx(seed["orgA"], seed["userB"])) as s:
        other = await _ids(s, "SELECT id FROM user_sessions")
    assert seed["sessionA"] not in other


async def test_fail_closed_without_context(seed):
    # No tenant context => GUCs unset => policies match nothing.
    async with session_scope(None) as s:
        count = (await s.execute(text("SELECT count(*) FROM organizations"))).scalar_one()
    assert count == 0


async def test_client_cannot_read_staff_users(seed):
    # A client subject in firm A must not see staff users (even in its own firm).
    client_ctx = TenantContext(
        organization_id=seed["orgA"], subject_type="client", subject_id=seed["userA"]
    )
    async with session_scope(client_ctx) as s:
        users = await _ids(s, "SELECT id FROM users")
    assert seed["userA"] not in users
