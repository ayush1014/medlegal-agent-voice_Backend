"""Increment 3 — Twilio Verify OTP (mocked transport, no real SMS) + rate limiting."""

from __future__ import annotations

import httpx
import pytest

from app.config import settings
from app.services import otp_service
from app.services.rate_limit import (
    RateLimitExceeded,
    SlidingWindowLimiter,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_start_verification_posts_and_returns_status():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(201, json={"status": "pending"})

    async with _client(handler) as client:
        status = await otp_service.start_verification("+15555550123", client=client)

    assert status == "pending"
    assert "/Verifications" in seen["url"]
    assert "To=%2B15555550123" in seen["body"]  # url-encoded +1555...


async def test_check_verification_approved_and_rejected():
    async def run(payload_status: str) -> bool:
        def handler(request):
            return httpx.Response(200, json={"status": payload_status})

        async with _client(handler) as client:
            return await otp_service.check_verification("+15555550123", "123456", client=client)

    assert await run("approved") is True
    assert await run("pending") is False


async def test_check_verification_404_is_failed_not_error():
    def handler(request):
        return httpx.Response(404, json={"code": 20404})

    async with _client(handler) as client:
        assert await otp_service.check_verification("+15555550123", "000000", client=client) is False


async def test_verify_error_raises():
    def handler(request):
        return httpx.Response(500, text="boom")

    async with _client(handler) as client:
        with pytest.raises(otp_service.OtpError):
            await otp_service.start_verification("+15555550123", client=client)


async def test_not_configured_raises(monkeypatch):
    monkeypatch.setattr(settings, "twilio_verify_service_sid", None)
    with pytest.raises(otp_service.OtpNotConfigured):
        await otp_service.start_verification("+15555550123")


def test_sliding_window_allows_then_blocks():
    lim = SlidingWindowLimiter()
    t = 1000.0
    for _ in range(3):
        lim.hit("k", limit=3, window_seconds=60, now=t)
    with pytest.raises(RateLimitExceeded):
        lim.hit("k", limit=3, window_seconds=60, now=t + 1)


def test_sliding_window_resets_after_window():
    lim = SlidingWindowLimiter()
    lim.hit("k", limit=1, window_seconds=60, now=100.0)
    with pytest.raises(RateLimitExceeded):
        lim.hit("k", limit=1, window_seconds=60, now=130.0)
    # After the window passes, it's allowed again.
    lim.hit("k", limit=1, window_seconds=60, now=200.0)
