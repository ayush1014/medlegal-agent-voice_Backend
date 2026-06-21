"""Wave 6 — returning-caller verification (name + DOB matching)."""

from __future__ import annotations

import uuid
from datetime import date

import pytest_asyncio
from sqlalchemy import text

from app.database import session_scope
from app.security.context import system_context
from app.services import verification_service as V


def test_name_matches():
    assert V.name_matches("John Doe", "john doe")
    assert V.name_matches("John Doe", "Doe, John")          # order-insensitive
    assert V.name_matches("John Michael Doe", "John Doe")   # first + last
    assert not V.name_matches("John Doe", "John")           # last name missing
    assert not V.name_matches("John Doe", "Jane Doe")       # first name wrong
    assert not V.name_matches("John Doe", "")
    assert not V.name_matches(None, "John Doe")


def test_dob_matches():
    assert V.dob_matches(date(1990, 5, 1), date(1990, 5, 1))
    assert not V.dob_matches(date(1990, 5, 1), date(1990, 5, 2))
    assert not V.dob_matches(None, date(1990, 5, 1))
    assert not V.dob_matches(date(1990, 5, 1), None)


def test_verify_requires_both():
    prior = {"full_name": "John Doe", "date_of_birth": date(1990, 5, 1)}
    assert V.verify(prior, "John Doe", date(1990, 5, 1))
    assert not V.verify(prior, "John Doe", date(1991, 5, 1))   # dob wrong
    assert not V.verify(prior, "Jane Doe", date(1990, 5, 1))   # name wrong


@pytest_asyncio.fixture
async def org(owner_engine):
    oid = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id,name,slug) VALUES (:o,'F',:s)"),
                        {"o": oid, "s": f"vf-{oid.hex[:8]}"})
    yield oid
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": oid})


async def test_find_prior_caller(org, owner_engine):
    phone = "+1" + f"{uuid.uuid4().int % 10**10:010d}"
    async with owner_engine.begin() as c:
        # a placeholder row (should be ignored) + a real prior lead with DOB
        await c.execute(text("INSERT INTO leads (organization_id,full_name,phone,case_type,source) "
                             "VALUES (:o,'Caller',:p,'Other Personal Injury','inbound_call')"),
                        {"o": org, "p": phone})
        await c.execute(text("INSERT INTO leads (organization_id,full_name,phone,case_type,source,"
                             "date_of_birth) VALUES (:o,'John Doe',:p,'Auto Accident','inbound_call',:dob)"),
                        {"o": org, "p": phone, "dob": date(1990, 5, 1)})

    async with session_scope(system_context(org)) as db:
        prior = await V.find_prior_caller(db, org, phone)
        missing = await V.find_prior_caller(db, org, "+19998887777")

    assert prior is not None
    assert prior["full_name"] == "John Doe" and prior["verifiable"] is True
    assert V.verify(prior, "john doe", date(1990, 5, 1))
    assert missing is None
