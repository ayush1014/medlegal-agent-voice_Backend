"""Follow-up automation trigger (staff/cron)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_staff_db, require_csrf
from app.services import followup_service

router = APIRouter(prefix="/followups", tags=["followups"])


@router.post("/run", dependencies=[Depends(require_csrf)])
async def run(db: AsyncSession = Depends(get_staff_db)) -> dict:
    """Advance + nudge this firm's leads. (A cron can call followup_service per org.)"""
    org = (await db.execute(text("SELECT current_setting('app.current_org')::uuid"))).scalar_one()
    return await followup_service.run_followups(org)
