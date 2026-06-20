"""OTP via Twilio Verify.

Twilio Verify owns the whole code lifecycle (generation, 10-min expiry, attempt
lockout), so we store nothing — we only start a verification and check a code.
Calls are async (httpx). A client can be injected for tests so no real SMS is
sent.
"""

from __future__ import annotations

import httpx

from app.config import settings

_VERIFY_BASE = "https://verify.twilio.com/v2/Services"


class OtpError(Exception):
    """Verify call failed unexpectedly."""


class OtpNotConfigured(OtpError):
    """Twilio Verify credentials are missing."""


def _require_config() -> tuple[str, str, str]:
    sid = settings.twilio_account_sid
    token = settings.twilio_auth_token
    service = settings.twilio_verify_service_sid
    if not (sid and token and service):
        raise OtpNotConfigured("Twilio Verify is not configured")
    return sid, token, service


async def start_verification(
    phone: str, *, channel: str = "sms", client: httpx.AsyncClient | None = None
) -> str:
    """Send an OTP to `phone`. Returns Twilio's verification status (e.g. 'pending')."""
    sid, token, service = _require_config()
    url = f"{_VERIFY_BASE}/{service}/Verifications"
    owns = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.post(
            url, data={"To": phone, "Channel": channel}, auth=(sid, token)
        )
    finally:
        if owns:
            await client.aclose()
    if resp.status_code >= 400:
        raise OtpError(f"Verify start failed ({resp.status_code})")
    return resp.json().get("status", "pending")


async def check_verification(
    phone: str, code: str, *, client: httpx.AsyncClient | None = None
) -> bool:
    """True iff the code is correct for a pending verification.

    Twilio returns 404 once a verification is consumed/expired — treated as a
    failed check (not an error) so we never leak why it failed.
    """
    sid, token, service = _require_config()
    url = f"{_VERIFY_BASE}/{service}/VerificationCheck"
    owns = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.post(
            url, data={"To": phone, "Code": code}, auth=(sid, token)
        )
    finally:
        if owns:
            await client.aclose()
    if resp.status_code == 404:
        return False
    if resp.status_code >= 400:
        raise OtpError(f"Verify check failed ({resp.status_code})")
    return resp.json().get("status") == "approved"
