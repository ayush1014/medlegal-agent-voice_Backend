# medlegal-agent-voice_Backend

FastAPI backend for the MedLegal AI personal-injury intake platform.

## Stack
- **FastAPI** + **Uvicorn** (ASGI)
- **SQLAlchemy 2.0** (async) + **asyncpg** → **Neon Postgres**
- **pydantic-settings** for config

## Project structure
```
app/
  main.py        # FastAPI app factory, CORS, lifespan
  config.py      # env-driven settings
  database.py    # async engine/session (lazy), Base, get_db dependency
  api/
    router.py    # aggregates feature routers
    health.py    # /api/health
```
Feature modules (models, schemas, services, routes for leads / ai / voice / sms /
documents / retainers) will be added on top of this skeleton.

## Setup
```bash
cd medlegal-agent-voice_Backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then fill in DATABASE_URL when ready
```

## Run
```bash
uvicorn app.main:app --reload
```
- API root:    http://localhost:8000/
- Health:      http://localhost:8000/api/health
- Swagger UI:  http://localhost:8000/docs

The app boots without a database. Database-backed routes activate once
`DATABASE_URL` is set in `.env`.
