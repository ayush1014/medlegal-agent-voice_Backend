"""In-memory sliding-window rate limiting.

Guards OTP requests (SMS-bomb / toll-fraud) and login attempts. Single-process
for MVP — fine for one app instance; swap the store for Redis when we scale out
horizontally (the call sites won't change).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from app.config import settings


class RateLimitExceeded(Exception):
    def __init__(self, retry_after_seconds: float):
        self.retry_after_seconds = max(1, round(retry_after_seconds))
        super().__init__("rate limit exceeded")


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window_seconds: float, now: float | None = None) -> None:
        """Record one hit for `key`; raise RateLimitExceeded if over `limit`
        within `window_seconds`."""
        now = time.monotonic() if now is None else now
        dq = self._hits[key]
        cutoff = now - window_seconds
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= limit:
            raise RateLimitExceeded(dq[0] + window_seconds - now)
        dq.append(now)


# Process-wide limiter.
limiter = SlidingWindowLimiter()


def guard_otp_request(phone: str, ip: str | None) -> None:
    """Throttle OTP sends per phone and per IP."""
    limiter.hit(f"otp:phone:{phone}", settings.otp_max_per_phone_per_hour, 3600)
    if ip:
        limiter.hit(f"otp:ip:{ip}", settings.otp_max_per_ip_per_hour, 3600)


def guard_login_attempt(identifier: str, ip: str | None) -> None:
    """Throttle login attempts per identifier (+IP)."""
    key = f"login:{identifier}:{ip or '-'}"
    limiter.hit(key, settings.login_max_per_identifier_per_15min, 900)
