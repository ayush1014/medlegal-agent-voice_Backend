"""CSRF protection via the double-submit-cookie pattern.

A non-HttpOnly `csrf_token` cookie is issued at login; the frontend echoes it in
an `X-CSRF-Token` header on state-changing requests. An attacker's cross-site
request can't read the cookie to forge the header, so the two won't match.
"""

from __future__ import annotations

import secrets

CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf(cookie_token: str | None, header_token: str | None) -> bool:
    if not cookie_token or not header_token:
        return False
    return secrets.compare_digest(cookie_token, header_token)
