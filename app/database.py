"""Async SQLAlchemy setup for Neon Postgres.

The engine is created lazily so the app boots even before `DATABASE_URL` is set.
Once the Neon connection string is provided in `.env`, the dependency below
yields a working async session.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


def _normalize_db_url(url: str) -> str:
    """Coerce a standard Postgres URL into the async (asyncpg) driver form.

    Neon hands out `postgresql://...` URLs; SQLAlchemy's async engine needs the
    `postgresql+asyncpg://` driver prefix. `sslmode` is a libpq param asyncpg
    doesn't accept, so it's dropped here (TLS is negotiated automatically with
    Neon over asyncpg).
    """
    if url.startswith("postgresql+asyncpg://"):
        normalized = url
    elif url.startswith("postgresql://"):
        normalized = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgres://"):
        normalized = url.replace("postgres://", "postgresql+asyncpg://", 1)
    else:
        normalized = url

    # Strip the libpq-only `sslmode` query param if present.
    if "sslmode=" in normalized:
        from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

        parts = urlparse(normalized)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "sslmode"]
        normalized = urlunparse(parts._replace(query=urlencode(query)))

    return normalized


# Lazily-initialized singletons.
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    global _engine, _sessionmaker
    if _engine is None:
        if not settings.database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. Add the Neon connection string to .env."
            )
        _engine = create_async_engine(
            _normalize_db_url(settings.database_url),
            echo=settings.debug,
            pool_pre_ping=True,
        )
        _sessionmaker = async_sessionmaker(
            bind=_engine, expire_on_commit=False, class_=AsyncSession
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine()  # initializes both
    assert _sessionmaker is not None
    return _sessionmaker


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a request-scoped async session."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session


async def dispose_engine() -> None:
    """Close the engine's connection pool on shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
