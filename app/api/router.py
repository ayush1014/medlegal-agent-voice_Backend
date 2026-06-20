"""Aggregate API router.

Feature routers (leads, ai, voice, sms, documents, retainers) get included here
as they're built, keeping `main.py` clean.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health
from app.api.routes import admin, auth, leads

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(leads.router)
