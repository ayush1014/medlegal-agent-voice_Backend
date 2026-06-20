"""Async SQLAlchemy setup for Neon Postgres.

The app connects as the least-privilege ``app_user`` role (see config) so
Row-Level Security applies. Each DB transaction is stamped with the current
tenant context via ``app.*`` GUCs, which the RLS policies read.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings
from app.security.context import TenantContext, get_current_context

logger = logging.getLogger(__name__)

# Query params understood only by libpq, not the asyncpg driver. TLS to Neon is
# negotiated automatically, so these are dropped during normalization.
_LIBPQ_ONLY_PARAMS = {"sslmode", "channel_binding"}


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


def _build_async_url(url: str) -> str:
    """Return the connection string in async-driver form.

    Neon issues standard ``postgresql://`` URLs; SQLAlchemy's async engine needs
    the ``postgresql+asyncpg://`` driver and asyncpg rejects libpq-only query
    params, so both are normalized here.
    """
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if k not in _LIBPQ_ONLY_PARAMS]
    return urlunparse(parsed._replace(query=urlencode(query)))


if not settings.rls_enforced:
    logger.warning(
        "APP_DB_PASSWORD is not set — connecting as the owner role. "
        "Row-Level Security is NOT enforced. Set it for any shared environment."
    )

engine = create_async_engine(
    _build_async_url(settings.runtime_database_url),
    echo=settings.debug,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def _apply_tenant_context(session: AsyncSession, ctx: TenantContext) -> None:
    """Push the tenant context into transaction-local ``app.*`` GUCs.

    Uses ``set_config(..., is_local => true)`` so the settings are scoped to the
    current transaction — safe with Neon's pooled (transaction-mode) connections.
    """
    await session.execute(
        text(
            "SELECT"
            " set_config('app.current_org', :org, true),"
            " set_config('app.current_subject_type', :stype, true),"
            " set_config('app.current_subject_id', :sid, true),"
            " set_config('app.current_role', :role, true)"
        ),
        {
            "org": str(ctx.organization_id),
            "stype": ctx.subject_type,
            "sid": str(ctx.subject_id) if ctx.subject_id else "",
            "role": ctx.role or "",
        },
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: a request-scoped session in a single transaction.

    The transaction is stamped with the current tenant context before any query
    runs, then committed on success / rolled back on error. Services should not
    open their own top-level transactions — flush within this unit of work.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            ctx = get_current_context()
            if ctx is not None:
                await _apply_tenant_context(session, ctx)
            yield session


@asynccontextmanager
async def session_scope(
    ctx: TenantContext | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """Standalone session in a single transaction, stamped with ``ctx``.

    For non-request callers (scripts, workers, tests). Mirrors ``get_db``.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            if ctx is not None:
                await _apply_tenant_context(session, ctx)
            yield session
