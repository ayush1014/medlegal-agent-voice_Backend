"""Increment 1 — auth primitives: passwords, JWTs, org-slug resolution."""

from __future__ import annotations

import uuid

import jwt
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import session_scope
from app.security.passwords import hash_password, needs_rehash, verify_password
from app.security.tokens import (
    ACCESS,
    REFRESH,
    AccessClaims,
    access_claims_from_payload,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.services.org_service import resolve_org_id


def test_password_hash_and_verify():
    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong", h) is False
    assert needs_rehash(h) is False
    assert verify_password("x", "not-a-hash") is False


def test_access_token_roundtrip():
    claims = AccessClaims(
        organization_id=uuid.uuid4(),
        subject_type="user",
        subject_id=uuid.uuid4(),
        role="admin",
    )
    token = create_access_token(claims)
    payload = decode_token(token, expected_use=ACCESS)
    back = access_claims_from_payload(payload)
    assert back == claims


def test_refresh_token_carries_session_and_identity():
    sid = uuid.uuid4()
    claims = AccessClaims(uuid.uuid4(), "client", uuid.uuid4(), None)
    token = create_refresh_token(sid, claims)
    payload = decode_token(token, expected_use=REFRESH)
    assert payload["jti"] == str(sid)
    assert access_claims_from_payload(payload) == claims


def test_token_use_is_enforced():
    token = create_access_token(
        AccessClaims(uuid.uuid4(), "user", uuid.uuid4(), "admin")
    )
    # Decoding an access token as a refresh token must fail.
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(token, expected_use=REFRESH)


@pytest_asyncio.fixture
async def org(owner_engine):
    oid = uuid.uuid4()
    slug = f"resolver-{oid.hex[:8]}"
    async with owner_engine.begin() as c:
        await c.execute(
            text("INSERT INTO organizations (id, name, slug) VALUES (:id,'R',:s)"),
            {"id": oid, "s": slug},
        )
    yield {"id": oid, "slug": slug}
    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": oid})


async def test_org_slug_resolves_without_tenant_context(org):
    # No tenant context (pre-auth): the SECURITY DEFINER resolver still works.
    async with session_scope(None) as s:
        resolved = await resolve_org_id(s, org["slug"])
        missing = await resolve_org_id(s, "does-not-exist")
    assert resolved == org["id"]
    assert missing is None
