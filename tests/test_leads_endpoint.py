"""Increment 5a — dashboard leads read endpoint, RLS-scoped via HTTP."""

from __future__ import annotations

import uuid

import httpx
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text

from app.main import app
from app.security.passwords import hash_password

PW = "password1234"


def _phone() -> str:
    return "+1" + f"{uuid.uuid4().int % 10**10:010d}"


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def seeded(owner_engine):
    """Two firms; firm A has an admin, two leads, and a client bound to lead 1."""
    d = {k: uuid.uuid4() for k in ("orgA", "orgB", "adminA", "adminB", "lead1", "lead2", "client1")}
    slugA, slugB = f"a-{d['orgA'].hex[:8]}", f"b-{d['orgB'].hex[:8]}"
    # Lowercase: the app normalizes emails on write, so seeds must match.
    admin_a_email, admin_b_email = "admin.a@example.com", "admin.b@example.com"
    client_email = "client1@example.com"

    async with owner_engine.begin() as c:
        for org, slug in ((d["orgA"], slugA), (d["orgB"], slugB)):
            await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:i,'F',:s)"),
                            {"i": org, "s": slug})
        for uid, org, email in ((d["adminA"], d["orgA"], admin_a_email), (d["adminB"], d["orgB"], admin_b_email)):
            await c.execute(
                text("INSERT INTO users (id,organization_id,email,password_hash,role) "
                     "VALUES (:i,:o,:e,:h,'owner')"),
                {"i": uid, "o": org, "e": email, "h": hash_password(PW)},
            )
        for lid, name in ((d["lead1"], "Lead One"), (d["lead2"], "Lead Two")):
            await c.execute(
                text("INSERT INTO leads (id,organization_id,full_name,phone,case_type) "
                     "VALUES (:i,:o,:n,:p,'Auto Accident')"),
                {"i": lid, "o": d["orgA"], "n": name, "p": _phone()},
            )
        await c.execute(
            text("INSERT INTO client_accounts (id,organization_id,lead_id,email,password_hash) "
                 "VALUES (:i,:o,:l,:e,:h)"),
            {"i": d["client1"], "o": d["orgA"], "l": d["lead1"], "e": client_email, "h": hash_password(PW)},
        )
    out = {**d, "slugA": slugA, "slugB": slugB, "adminA_email": admin_a_email,
           "adminB_email": admin_b_email, "client_email": client_email}
    yield out
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id = ANY(:ids)"),
                        {"ids": [d["orgA"], d["orgB"]]})


async def _login(client, slug, email):
    r = await client.post("/api/auth/login", headers={"X-Org-Slug": slug},
                          json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return {"X-Org-Slug": slug}


async def test_admin_sees_whole_firm(client, seeded):
    h = await _login(client, seeded["slugA"], seeded["adminA_email"])
    r = await client.get("/api/leads", headers=h)
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()}
    assert {str(seeded["lead1"]), str(seeded["lead2"])}.issubset(ids)


async def test_client_sees_only_own_lead(client, seeded):
    h = await _login(client, seeded["slugA"], seeded["client_email"])
    r = await client.get("/api/leads", headers=h)
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()]
    assert ids == [str(seeded["lead1"])]
    # Detail of the other firm-mate lead is not visible to the client.
    nope = await client.get(f"/api/leads/{seeded['lead2']}", headers=h)
    assert nope.status_code == 404


async def test_other_firm_cannot_see(client, seeded):
    h = await _login(client, seeded["slugB"], seeded["adminB_email"])
    r = await client.get("/api/leads", headers=h)
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()}
    assert str(seeded["lead1"]) not in ids and str(seeded["lead2"]) not in ids


async def test_unauthenticated_rejected(client, seeded):
    r = await client.get("/api/leads", headers={"X-Org-Slug": seeded["slugA"]})
    assert r.status_code == 401


async def test_camelcase_shape(client, seeded):
    h = await _login(client, seeded["slugA"], seeded["adminA_email"])
    row = (await client.get("/api/leads", headers=h)).json()[0]
    for key in ("fullName", "caseType", "leadScore", "leadTemperature", "pipelineStatus", "updatedAt"):
        assert key in row
