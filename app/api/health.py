"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, object]:
    """Liveness check — confirms the process is up."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.environment,
        "database_configured": settings.database_url is not None,
    }
