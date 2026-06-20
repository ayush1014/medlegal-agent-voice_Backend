"""Increment 4 — auth endpoints end-to-end through the ASGI app.

Twilio Verify is monkeypatched (no real SMS). RLS runs for real (app_user).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text

from app.config import settings
from app.main import app
from app.services import otp_service

PASSWORD = "password1234"


def _phone() -> str:
    return "+1" + f"{uuid.uuid4().int % 10**10:010d}"


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(autouse=True)
def otp_ok(monkeypatch):
    """Make Twilio Verify always succeed without sending SMS."""
    async def _start(phone, **kw):
        return "pending"

    async def _check(phone, code, **kw):
        return code == "000000"

    monkeypatch.setattr(otp_service, "start_verification", _start)
    monkeypatch.setattr(otp_service, "check_verification", _check)


@pytest_asyncio.fixture
async def firm(client, owner_engine, monkeypatch):
    """Provision a firm + admin via the internal endpoint; clean up after."""
    monkeypatch.setattr(settings, "provision_secret", "test-secret")
    slug = f"firm-{uuid.uuid4().hex[:8]}"
    admin_email = f"admin-{uuid.uuid4().hex[:6]}@example.com"
    admin_phone = _phone()

    resp = await client.post(
        "/api/admin/provision",
        headers={"X-Provision-Secret": "test-secret"},
        json={
            "org_name": "Test Firm",
            "org_slug": slug,
            "admin_email": admin_email,
            "admin_phone": admin_phone,
            "admin_password": PASSWORD,
            "intake_phone": _phone(),
        },
    )
    assert resp.status_code == 200, resp.text
    org_id = uuid.UUID(resp.json()["organization_id"])
    yield {"slug": slug, "org_id": org_id, "admin_email": admin_email, "admin_phone": admin_phone}

    async with owner_engine.begin() as c:
        await c.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": org_id})


def _h(firm, **extra):
    return {"X-Org-Slug": firm["slug"], **extra}


# --- Provisioning + admin login ---

async def test_provision_requires_secret(client):
    # No secret configured by default → endpoint hidden (404).
    r = await client.post("/api/admin/provision", json={
        "org_name": "X", "org_slug": "x", "admin_email": "a@example.com", "admin_password": PASSWORD,
    })
    assert r.status_code == 404


async def test_admin_login_email_password_and_me(client, firm):
    r = await client.post("/api/auth/login", headers=_h(firm),
                          json={"email": firm["admin_email"], "password": PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json()["subject_type"] == "user"
    assert "access_token" in r.cookies and "csrf_token" in r.cookies

    me = await client.get("/api/auth/me", headers=_h(firm))
    assert me.status_code == 200
    body = me.json()
    assert body["subject_type"] == "user" and body["role"] == "owner"
    assert body["organization_id"] == str(firm["org_id"])


async def test_admin_login_by_phone_otp(client, firm):
    r = await client.post("/api/auth/otp/verify", headers=_h(firm),
                          json={"phone": firm["admin_phone"], "code": "000000"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["authenticated"] is True and body["subject_type"] == "user"


async def test_bad_otp_code_rejected(client, firm):
    r = await client.post("/api/auth/otp/verify", headers=_h(firm),
                          json={"phone": firm["admin_phone"], "code": "999999"})
    assert r.status_code == 401


# --- Client signup + login ---

async def test_client_signup_then_logins(client, firm):
    new_phone = _phone()
    new_email = f"client-{uuid.uuid4().hex[:6]}@example.com"

    # New phone → verified but no account → signup token.
    v = await client.post("/api/auth/otp/verify", headers=_h(firm),
                          json={"phone": new_phone, "code": "000000"})
    assert v.status_code == 200 and v.json()["authenticated"] is False
    signup_token = v.json()["signup_token"]
    assert signup_token

    s = await client.post("/api/auth/client/signup", headers=_h(firm), json={
        "signup_token": signup_token, "email": new_email, "password": PASSWORD,
        "full_name": "Jane Patient", "case_type": "Auto Accident",
        "incident_description": "Rear-ended", "incident_location": "Atlanta, GA",
        "injury_area": "Neck",
    })
    assert s.status_code == 200, s.text
    assert s.json()["subject_type"] == "client"

    me = await client.get("/api/auth/me", headers=_h(firm))
    assert me.json()["subject_type"] == "client"

    # Email+password login as the client.
    await client.post("/api/auth/logout", headers=_h(firm, **_csrf(client)))
    r = await client.post("/api/auth/login", headers=_h(firm),
                          json={"email": new_email, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["subject_type"] == "client"

    # Phone+OTP login as the same client.
    r2 = await client.post("/api/auth/otp/verify", headers=_h(firm),
                           json={"phone": new_phone, "code": "000000"})
    assert r2.status_code == 200 and r2.json()["authenticated"] is True
    assert r2.json()["subject_type"] == "client"


async def test_claim_lead_from_call(client, firm, owner_engine):
    # A call-created lead with a phone, no client account yet.
    call_phone = _phone()
    lead_id = uuid.uuid4()
    async with owner_engine.begin() as c:
        await c.execute(
            text("INSERT INTO leads (id, organization_id, full_name, phone, case_type, source) "
                 "VALUES (:id,:o,'Caller',:p,'Auto Accident','inbound_call')"),
            {"id": lead_id, "o": firm["org_id"], "p": call_phone},
        )

    r = await client.post("/api/auth/otp/verify", headers=_h(firm),
                          json={"phone": call_phone, "code": "000000"})
    assert r.status_code == 200 and r.json()["authenticated"] is True
    assert r.json()["subject_type"] == "client"

    # Exactly one client account, bound to the call-created lead (no duplicate).
    async with owner_engine.begin() as c:
        rows = (await c.execute(
            text("SELECT lead_id FROM client_accounts WHERE organization_id=:o AND phone=:p"),
            {"o": firm["org_id"], "p": call_phone},
        )).scalars().all()
    assert rows == [lead_id]


# --- Sessions: refresh, CSRF, logout-all ---

def _csrf(client) -> dict:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


async def test_refresh_requires_csrf_and_rotates(client, firm):
    await client.post("/api/auth/login", headers=_h(firm),
                      json={"email": firm["admin_email"], "password": PASSWORD})

    # Without CSRF header → 403.
    no_csrf = await client.post("/api/auth/refresh", headers=_h(firm))
    assert no_csrf.status_code == 403

    # With CSRF → rotates, still authenticated.
    ok = await client.post("/api/auth/refresh", headers=_h(firm, **_csrf(client)))
    assert ok.status_code == 200
    me = await client.get("/api/auth/me", headers=_h(firm))
    assert me.status_code == 200


async def test_logout_all_then_refresh_fails(client, firm):
    await client.post("/api/auth/login", headers=_h(firm),
                      json={"email": firm["admin_email"], "password": PASSWORD})
    # Capture the live tokens before logout-all clears the cookie jar.
    old_refresh = client.cookies.get("refresh_token")
    old_csrf = client.cookies.get("csrf_token")

    out = await client.post("/api/auth/logout-all", headers=_h(firm, **_csrf(client)))
    assert out.status_code == 200

    # Replay the now-revoked refresh token WITH valid CSRF: must still fail (401),
    # proving revocation — not just the CSRF guard — blocks it.
    r = await client.post(
        "/api/auth/refresh",
        headers=_h(firm, **{"X-CSRF-Token": old_csrf}),
        cookies={"refresh_token": old_refresh, "csrf_token": old_csrf},
    )
    assert r.status_code == 401


# --- Hardening ---

async def test_login_enumeration_uniform(client, firm):
    wrong_pw = await client.post("/api/auth/login", headers=_h(firm),
                                 json={"email": firm["admin_email"], "password": "wrong-password"})
    unknown = await client.post("/api/auth/login", headers=_h(firm),
                                json={"email": "nobody@example.com", "password": PASSWORD})
    assert wrong_pw.status_code == 401 and unknown.status_code == 401
    assert wrong_pw.json()["detail"] == unknown.json()["detail"]


async def test_unknown_firm_fails_closed(client):
    r = await client.post("/api/auth/login",
                          headers={"X-Org-Slug": "no-such-firm"},
                          json={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 400

    r2 = await client.post("/api/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    assert r2.status_code == 400  # no org at all
