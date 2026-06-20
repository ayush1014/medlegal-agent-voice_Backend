"""Provision the least-privilege application role (``app_user``).

Idempotent. Connects as the OWNER (``DATABASE_URL``) and:
  - creates ``app_user`` if missing (LOGIN, NOBYPASSRLS),
  - (re)sets its password from ``APP_DB_PASSWORD`` — also use this to rotate,
  - grants schema usage + CRUD on existing tables,
  - sets default privileges so future tables auto-grant.

Run this ONCE before the first migration, and again whenever you rotate the
password:

    APP_DB_PASSWORD=... python -m scripts.provision_app_role
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import text

from app.config import settings
from app.database import _build_async_url
from sqlalchemy.ext.asyncio import create_async_engine

APP_USER = settings.app_db_user


async def provision(password: str) -> None:
    # Postgres utility statements (CREATE/ALTER ROLE) can't take bound params over
    # asyncpg, so the password is inlined as an escaped string literal. The role
    # name comes from config (not user input); the password is escaped defensively.
    pw_literal = "'" + password.replace("'", "''") + "'"

    # Owner connection (can create roles + grant).
    engine = create_async_engine(_build_async_url(settings.database_url))
    try:
        async with engine.begin() as conn:
            exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :n"),
                    {"n": APP_USER},
                )
            ).scalar()

            if not exists:
                await conn.execute(
                    text(
                        f'CREATE ROLE "{APP_USER}" LOGIN NOSUPERUSER NOCREATEDB '
                        f"NOCREATEROLE NOBYPASSRLS PASSWORD {pw_literal}"
                    )
                )
            else:
                await conn.execute(
                    text(f'ALTER ROLE "{APP_USER}" WITH PASSWORD {pw_literal}')
                )

            await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{APP_USER}"'))
            await conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                    f'IN SCHEMA public TO "{APP_USER}"'
                )
            )
            await conn.execute(
                text(
                    f"GRANT USAGE, SELECT ON ALL SEQUENCES "
                    f'IN SCHEMA public TO "{APP_USER}"'
                )
            )
            await conn.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{APP_USER}"'
                )
            )
            await conn.execute(
                text(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                    f'GRANT USAGE, SELECT ON SEQUENCES TO "{APP_USER}"'
                )
            )
    finally:
        await engine.dispose()


def main() -> None:
    password = os.environ.get("APP_DB_PASSWORD") or settings.app_db_password
    if not password:
        sys.exit("APP_DB_PASSWORD is required (set it in the environment or .env).")
    asyncio.run(provision(password))
    print(f"Provisioned role '{APP_USER}' (NOBYPASSRLS) with CRUD on public schema.")


if __name__ == "__main__":
    main()
