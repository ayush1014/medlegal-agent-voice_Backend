"""Application settings.

Values are read from environment variables (and a local `.env` file in dev).
Everything has a sensible default so the app boots before the Neon database URL
and provider keys are supplied.
"""

from __future__ import annotations

from functools import lru_cache

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
    # Left empty until the Neon connection string is provided. The app still
    # boots without it; database-backed routes will report it's not configured.
    database_url: str | None = Field(default=None)

    # --- CORS --------------------------------------------------------------
    # Origins allowed to call the API. The Next.js frontend runs on 3000/3001.
    cors_origins: list[str] = Field(
        default=[
            "http://localhost:3000",
            "http://localhost:3001",
        ]
    )

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so settings are parsed once per process."""
    return Settings()


settings = get_settings()
