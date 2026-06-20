"""Aggregate API router.

Feature routers (leads, ai, voice, sms, documents, retainers) get included here
as they're built, keeping `main.py` clean.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health

api_router = APIRouter()
api_router.include_router(health.router)
