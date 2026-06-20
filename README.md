# medlegal-agent-voice_Backend

FastAPI backend for the MedLegal AI personal-injury intake platform.

## Stack
- **FastAPI** + **Uvicorn** (ASGI)
- **SQLAlchemy 2.0** (async) + **asyncpg** → **Neon Postgres** (v18)
- **Alembic** (async) for migrations
- **pydantic-settings** for config

## Project structure
```
app/
  main.py            # FastAPI app factory, CORS, lifespan
  config.py          # env-driven settings (owner + app-role connection)
  database.py        # async engine/session, get_db (per-request tenant GUCs)
  security/
    context.py       # request-scoped TenantContext (drives RLS)
  models/            # ORM models + enums (controlled vocabularies)
  api/
    router.py        # aggregates feature routers
    health.py        # /api/health
migrations/          # Alembic (async) — versions/ holds each migration
scripts/
  provision_app_role.py  # creates/rotates the least-privilege app_user role
tests/               # RLS isolation tests
```

## Database & security model

Two Postgres roles, one database:
- **Owner** (`DATABASE_URL`) — runs migrations and admin tasks. Bypasses RLS.
- **`app_user`** (least-privilege, `NOBYPASSRLS`) — the role the app connects as
  at runtime, so **Row-Level Security is actually enforced**. The runtime URL is
  derived from `DATABASE_URL` (same host/db) with `app_user` + `APP_DB_PASSWORD`.

Every request stamps the tenant context (`organization_id`, subject, role) into
transaction-local `app.*` GUCs; RLS policies read them and are **fail-closed**
(no context → no rows).

## Environment (`.env`)
```
APP_NAME=MedLegal Intake API
ENVIRONMENT=development
DEBUG=true
API_V1_PREFIX=/api

# Owner connection (migrations/admin). Bare postgresql:// URL.
DATABASE_URL=postgresql://OWNER:PASSWORD@HOST/neondb?sslmode=require

# Password for the least-privilege app_user role (you choose this value).
# Without it the app falls back to the owner role and RLS is NOT enforced.
APP_DB_PASSWORD=choose-a-strong-password
```

## Setup
```bash
cd medlegal-agent-voice_Backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 1) Create / rotate the least-privilege role (reads APP_DB_PASSWORD)
python -m scripts.provision_app_role

# 2) Apply migrations (runs as the owner)
alembic upgrade head
```

## Run
```bash
uvicorn app.main:app --reload
```
- API root:    http://localhost:8000/
- Health:      http://localhost:8000/api/health
- Swagger UI:  http://localhost:8000/docs

## Tests
```bash
python -m pytest          # RLS isolation suite
```

## Migrations (Alembic)
```bash
alembic upgrade head          # apply
alembic downgrade -1          # roll back one
alembic check                 # detect model/DB drift
alembic revision -m "msg"     # new migration
```
