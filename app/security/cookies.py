"""Auth cookie management.

Access + refresh tokens are HttpOnly (invisible to JS); the CSRF token is
readable by JS so the frontend can echo it in a header. The refresh cookie is
scoped to the auth path so it's only sent where it's needed.
"""

from __future__ import annotations

from fastapi import Response

from app.config import settings
from app.security.csrf import CSRF_COOKIE_NAME

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
REFRESH_PATH = "/api/auth"


def _common(http_only: bool, max_age: int, path: str) -> dict:
    kwargs = dict(
        httponly=http_only,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=max_age,
        path=path,
    )
    if settings.cookie_domain:
        kwargs["domain"] = settings.cookie_domain
    return kwargs


def set_auth_cookies(
    response: Response, *, access_token: str, refresh_token: str, csrf_token: str
) -> None:
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        **_common(True, settings.access_token_ttl_seconds, "/"),
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        **_common(True, settings.refresh_token_ttl_seconds, REFRESH_PATH),
    )
    # Readable by JS (not HttpOnly) so the SPA can echo it back as a header.
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        **_common(False, settings.refresh_token_ttl_seconds, "/"),
    )


def set_csrf_cookie(response: Response, csrf_token: str) -> None:
    """Set just the (JS-readable) CSRF cookie — used to bootstrap a token for a
    page that reloaded and lost its in-memory copy."""
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        **_common(False, settings.refresh_token_ttl_seconds, "/"),
    )


def clear_auth_cookies(response: Response) -> None:
    domain = settings.cookie_domain
    response.delete_cookie(ACCESS_COOKIE, path="/", domain=domain)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH, domain=domain)
    response.delete_cookie(CSRF_COOKIE_NAME, path="/", domain=domain)
