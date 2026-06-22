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
    # Signing secret for session JWTs. Required (no default) so startup fails
    # fast if unset. pydantic-settings reads it from the JWT_SECRET env var.
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 60 * 15  # 15 minutes
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30  # 30 days
    # Cookie scope. None => host-only cookies (fine for localhost). In prod set
    # ".medlegal.app" so the cookie is shared across firm subdomains.
    cookie_domain: str | None = None
    cookie_secure: bool = False  # set true in prod (HTTPS); HSTS at the edge
    # "lax" for same-site (local). For a cross-site SPA (frontend on a different
    # domain, e.g. Vercel) set "none" — REQUIRES cookie_secure=true.
    cookie_samesite: str = "lax"
    # Base domain used to derive the firm slug from the request host in prod.
    base_domain: str | None = None  # e.g. "medlegal.app"

    # --- Twilio Verify (OTP) ----------------------------------------------
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_verify_service_sid: str | None = None
    # Twilio WhatsApp sender (bare E.164, no "whatsapp:" prefix). The funnel uses
    # WhatsApp for follow-ups + document gathering (SMS A2P not provisioned).
    # Twilio sandbox sender is +14155238886.
    twilio_whatsapp_number: str | None = None
    # Channel for follow-up / document / retainer messages: "whatsapp" or "sms".
    # SMS needs no template approval (set FUNNEL_CHANNEL=sms to use it now); WhatsApp
    # needs approved templates for business-initiated sends. US SMS at volume needs A2P.
    funnel_channel: str = "whatsapp"
    # Approved WhatsApp Content template SIDs (HX…). REQUIRED for business-initiated
    # messages (outside the 24h window WhatsApp rejects freeform with error 63016).
    # Create them in Twilio Content Template Builder; see docs/whatsapp_templates.md.
    whatsapp_template_doc_request: str | None = None   # vars: 1=firm 2=checklist 3=link
    whatsapp_template_retainer: str | None = None       # vars: 1=firm 2=link
    whatsapp_template_nudge: str | None = None          # vars: 1=firm

    # --- Email (MVP doc-intake + retainer via Gmail SMTP/IMAP) -----------
    # When set, document requests + retainers go by EMAIL (clients reply with their
    # files; an IMAP poller ingests attachments → GCS). Use a Gmail App Password.
    gmail_user: str | None = None
    gmail_app_password: str | None = None
    gmail_from_name: str = "medLegal"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    imap_host: str = "imap.gmail.com"
    email_poll_seconds: int = 60          # inbound IMAP poll cadence
    email_inbound_enabled: bool = True    # run the inbound poller (needs gmail creds)

    # --- Rate limiting (abuse / SMS-bomb / toll-fraud guards) -------------
    otp_max_per_phone_per_hour: int = 5
    otp_max_per_ip_per_hour: int = 30
    login_max_per_identifier_per_15min: int = 10

    # --- Voice / AI providers (PRD-2) -------------------------------------
    # DeepSeek (OpenAI-compatible) drives the intake LLM + post-call extraction.
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    # Post-call extraction (latency irrelevant) → thinking model for accuracy.
    deepseek_model: str = "deepseek-v4-pro"
    # Realtime conversation → non-thinking model for low turn latency.
    deepseek_realtime_model: str = "deepseek-v4-flash"

    # Deepgram Nova 3 (STT) + Aura 2 (TTS).
    deepgram_api_key: str | None = None

    # LiveKit (realtime rooms + SIP ingress).
    livekit_url: str | None = None
    livekit_api_key: str | None = None
    livekit_api_secret: str | None = None
    # LiveKit SIP host the Twilio call is bridged into (e.g. "xxxx.sip.livekit.cloud").
    # Until set, inbound calls fall back to voicemail capture.
    livekit_sip_uri: str | None = None

    # Public base URL Twilio calls back for status/recording (ngrok in dev).
    public_base_url: str | None = None
    # Verify inbound Twilio webhook signatures (disable only in tests/local).
    twilio_validate_webhooks: bool = True
    # Frontend base URL for client-portal deep links in SMS (e.g. http://localhost:3000).
    frontend_base_url: str | None = None

    # --- Background jobs (follow-up automation) ---------------------------
    # Run the follow-up sweep inside the API process on an interval. Leave OFF
    # in multi-instance deploys (use the `python -m app.jobs.followups` cron
    # instead) so the tick runs exactly once.
    followups_scheduler_enabled: bool = False
    followups_interval_seconds: int = 900  # 15 minutes

    # Post-call processor: drains `call.ended` outbox events (extraction → memory →
    # intelligence) inside the API process, OFF the voice worker's shutdown path so
    # a hangup can never kill it.
    post_call_worker_enabled: bool = True
    post_call_interval_seconds: int = 5

    # OpenAI embeddings for RAG memory (dimension lives in models.enums).
    openai_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"

    # Realtime conversation + dashboard chat QA → OpenAI gpt-4o-mini (fast).
    # Post-call extraction uses DeepSeek `deepseek_model` (v4-pro, thinking) for accuracy.
    voice_llm_model: str = "gpt-4o-mini"

    # Firm timezone — anchors the "today" the voice agent uses to resolve relative
    # dates a caller gives ("last Tuesday", "a couple weeks ago").
    firm_timezone: str = "America/New_York"

    # GCS storage for recordings/documents.
    storage_backend: str = "gcs"
    gcs_bucket_name: str | None = None
    google_cloud_project: str | None = None
    google_application_credentials_json: str | None = None

    # --- Signup / provisioning --------------------------------------------
    # Short-lived token proving a phone was OTP-verified, to complete signup.
    signup_token_ttl_seconds: int = 600  # 10 minutes
    # Shared secret gating the internal admin-provision endpoint. If unset, the
    # endpoint is disabled (returns 404) — there is no public admin signup.
    provision_secret: str | None = None

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def email_enabled(self) -> bool:
        """True when Gmail SMTP/IMAP credentials are configured."""
        return bool(self.gmail_user and self.gmail_app_password)

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
