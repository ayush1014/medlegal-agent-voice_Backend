"""Increment 2 — refresh-session rotation, reuse detection, CSRF."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import session_scope
from app.security.context import TenantContext
from app.security.csrf import generate_csrf_token, verify_csrf
from app.security.tokens import AccessClaims
from app.services import session_service


def test_csrf_double_submit():
    tok = generate_csrf_token()
    assert verify_csrf(tok, tok) is True
    assert verify_csrf(tok, "other") is False
    assert verify_csrf(None, tok) is False
    assert verify_csrf(tok, None) is False


@pytest_asyncio.fixture
async def admin(owner_engine):
    org, uid = uuid.uuid4(), uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(
            text("INSERT INTO organizations (id, name, slug) VALUES (:o,'S',:s)"),
            {"o": org, "s": f"sess-{org.hex[:8]}"},
        )
        await c.execute(
            text("INSERT INTO users (id, organization_id, email, role) "
                 "VALUES (:u,:o,'a@sess.test','owner')"),
            {"u": uid, "o": org},
        )
    yield {"org": org, "uid": uid}
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": org})


def _claims(admin) -> AccessClaims:
    return AccessClaims(admin["org"], "user", admin["uid"], "owner")


def _ctx(admin) -> TenantContext:
    return TenantContext(admin["org"], "user", admin["uid"], "owner")


async def _live_session_count(admin) -> int:
    async with session_scope(_ctx(admin)) as db:
        return (await db.execute(
            text("SELECT count(*) FROM user_sessions WHERE revoked_at IS NULL")
        )).scalar_one()


async def test_create_session_persists_row(admin):
    async with session_scope(_ctx(admin)) as db:
        issued = await session_service.create_session(db, _claims(admin))
    assert issued.access_token and issued.refresh_token
    assert await _live_session_count(admin) == 1


async def test_rotation_revokes_old_and_issues_new(admin):
    async with session_scope(_ctx(admin)) as db:
        first = await session_service.create_session(db, _claims(admin))
    async with session_scope(_ctx(admin)) as db:
        _, second = await session_service.rotate_session(db, first.refresh_token)
    # Old token no longer rotates; still exactly one live session.
    assert second.refresh_token != first.refresh_token
    assert await _live_session_count(admin) == 1


async def test_reuse_detection_revokes_chain(admin):
    async with session_scope(_ctx(admin)) as db:
        first = await session_service.create_session(db, _claims(admin))
    async with session_scope(_ctx(admin)) as db:
        await session_service.rotate_session(db, first.refresh_token)  # first now revoked

    # Replaying the already-rotated token is treated as theft → revoke everything.
    with pytest.raises(session_service.SessionError):
        async with session_scope(_ctx(admin)) as db:
            await session_service.rotate_session(db, first.refresh_token)

    assert await _live_session_count(admin) == 0


async def test_revoke_all(admin):
    async with session_scope(_ctx(admin)) as db:
        await session_service.create_session(db, _claims(admin))
        await session_service.create_session(db, _claims(admin))
    assert await _live_session_count(admin) == 2
    async with session_scope(_ctx(admin)) as db:
        await session_service.revoke_all(db, "user", admin["uid"])
    assert await _live_session_count(admin) == 0
