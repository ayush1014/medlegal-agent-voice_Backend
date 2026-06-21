"""Aggregate API router.

Feature routers (leads, ai, voice, sms, documents, retainers) get included here
as they're built, keeping `main.py` clean.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health
from app.api.routes import (
    admin, analytics, auth, documents, followups, leads, messaging, org, portal,
    retainers, voice,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(leads.router)
api_router.include_router(voice.router)
api_router.include_router(documents.router)
api_router.include_router(messaging.router)
api_router.include_router(retainers.router)
api_router.include_router(followups.router)
api_router.include_router(portal.router)
api_router.include_router(analytics.router)
api_router.include_router(org.router)
