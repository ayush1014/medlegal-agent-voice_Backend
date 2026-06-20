"""Application settings.

Values are read from environment variables (and a local `.env` file in dev),
validated once at startup. Required settings have no default so a misconfigured
environment fails fast rather than at first request.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---------------------------------------------------------------
    app_name: str = "MedLegal Intake API"
    environment: str = "development"  # development | staging | production
    debug: bool = True
    api_v1_prefix: str = "/api"

    # --- Database (Neon Postgres) -----------------------------------------
    # Required. Owner connection string — used for migrations and admin tasks.
    database_url: str

    # Least-privilege application role. The app connects as this role at runtime
    # so Row-Level Security actually applies (the owner role bypasses RLS). The
    # runtime URL is derived from `database_url` (same host/db) with these creds;
    # only the password needs to be supplied. Provision the role with
    # `scripts/provision_app_role.py`.
    app_db_user: str = "app_user"
    app_db_password: str | None = Field(default=None)

    # --- CORS --------------------------------------------------------------
    # Origins allowed to call the API. The Next.js frontend runs on 3000/3001.
    cors_origins: list[str] = Field(
        default=[
            "http://localhost:3000",
            "http://localhost:3001",
        ]
    )

    # --- Auth: JWT & cookies ----------------------------------------------
    # Signing secret for session JWTs. MUST be overridden in any shared/prod env.
    jwt_secret: str = "dev-insecure-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 60 * 15  # 15 minutes
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30  # 30 days
    # Cookie scope. None => host-only cookies (fine for localhost). In prod set
    # ".medlegal.app" so the cookie is shared across firm subdomains.
    cookie_domain: str | None = None
    cookie_secure: bool = False  # set true in prod (HTTPS); HSTS at the edge
    # Base domain used to derive the firm slug from the request host in prod.
    base_domain: str | None = None  # e.g. "medlegal.app"

    # --- Twilio Verify (OTP) ----------------------------------------------
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_verify_service_sid: str | None = None

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def rls_enforced(self) -> bool:
        """True when the app connects as the least-privilege role (RLS active)."""
        return self.app_db_password is not None

    @property
    def runtime_database_url(self) -> str:
        """Connection string the app uses at runtime.

        When `app_db_password` is set, swaps the owner credentials in
        `database_url` for the `app_user` role so RLS is enforced. Otherwise
        falls back to the owner URL (dev convenience — RLS will NOT apply).
        """
        if not self.app_db_password:
            return self.database_url

        parts = urlsplit(self.database_url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        netloc = f"{self.app_db_user}:{quote(self.app_db_password, safe='')}@{host}{port}"
        return urlunsplit(
            (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
        )


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so settings are parsed once per process."""
    return Settings()


settings = get_settings()
