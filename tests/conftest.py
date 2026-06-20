"""Shared test fixtures.

`owner_engine` connects as the owner (bypasses RLS) purely to seed/tear down
fixture data. The tests themselves query through the app engine (``app_user``),
which is fully constrained by RLS — that's the whole point.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.database import _build_async_url


@pytest_asyncio.fixture(autouse=True)
async def _dispose_app_engine():
    """Each test runs on its own event loop; dispose the app engine's pool after
    each so no connection outlives the loop that created it."""
    from app.database import engine

    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def owner_engine():
    engine = create_async_engine(_build_async_url(settings.database_url))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(owner_engine):
    """Two isolated firms (A and B), each with a user, phone number and session."""
    ids = {
        "orgA": uuid.uuid4(),
        "orgB": uuid.uuid4(),
        "userA": uuid.uuid4(),
        "userB": uuid.uuid4(),
        "phoneA": uuid.uuid4(),
        "phoneB": uuid.uuid4(),
        "sessionA": uuid.uuid4(),
        "sessionB": uuid.uuid4(),
    }

    async with owner_engine.begin() as conn:
        for org, slug in ((ids["orgA"], "firm-a"), (ids["orgB"], "firm-b")):
            await conn.execute(
                text(
                    "INSERT INTO organizations (id, name, slug) "
                    "VALUES (:id, :name, :slug)"
                ),
                {"id": org, "name": str(slug), "slug": f"{slug}-{org.hex[:8]}"},
            )
        for uid, org, email in (
            (ids["userA"], ids["orgA"], "a@firm-a.test"),
            (ids["userB"], ids["orgB"], "b@firm-b.test"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO users (id, organization_id, email, role) "
                    "VALUES (:id, :org, :email, 'owner')"
                ),
                {"id": uid, "org": org, "email": email},
            )
        for pid, org, e164 in (
            (ids["phoneA"], ids["orgA"], "+15550000001"),
            (ids["phoneB"], ids["orgB"], "+15550000002"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO phone_numbers (id, organization_id, e164) "
                    "VALUES (:id, :org, :e164)"
                ),
                {"id": pid, "org": org, "e164": e164},
            )
        for sid, org, subj in (
            (ids["sessionA"], ids["orgA"], ids["userA"]),
            (ids["sessionB"], ids["orgB"], ids["userB"]),
        ):
            await conn.execute(
                text(
                    "INSERT INTO user_sessions "
                    "(id, organization_id, subject_type, subject_id, "
                    " refresh_token_hash, expires_at) "
                    "VALUES (:id, :org, 'user', :subj, 'x', now() + interval '1 day')"
                ),
                {"id": sid, "org": org, "subj": subj},
            )

    yield ids

    # Teardown — deleting the orgs cascades to users/phones/sessions.
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM organizations WHERE id = ANY(:ids)"),
            {"ids": [ids["orgA"], ids["orgB"]]},
        )
