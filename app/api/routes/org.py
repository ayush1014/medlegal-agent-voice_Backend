"""Firm settings — profile (editable) + integration/config status (read-only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_staff_db, require_csrf
from app.config import settings

router = APIRouter(prefix="/org", tags=["org"])


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_staff_db)) -> dict:
    org = (await db.execute(
        text("SELECT name, slug, intake_phone_e164 FROM organizations LIMIT 1"))).first()
    numbers = [dict(r._mapping) for r in (await db.execute(
        text("SELECT e164, is_primary FROM phone_numbers ORDER BY is_primary DESC"))).all()]
    return {
        "profile": {
            "name": org.name if org else None,
            "slug": org.slug if org else None,
            "intake_phone": org.intake_phone_e164 if org else None,
        },
        "phone_numbers": numbers,
        "integrations": {
            "whatsapp_sender_configured": bool(settings.twilio_whatsapp_number),
            "voice_bridge_configured": bool(settings.livekit_sip_uri),
            "twilio_configured": bool(settings.twilio_account_sid),
            "realtime_model": settings.deepseek_realtime_model,
            "extraction_model": settings.deepseek_model,
            "embedding_model": settings.embedding_model,
        },
    }


class OrgPatch(BaseModel):
    name: str | None = None
    intake_phone: str | None = None


@router.patch("/settings", dependencies=[Depends(require_csrf)])
async def patch_settings(body: OrgPatch, db: AsyncSession = Depends(get_staff_db)) -> dict:
    updates: dict = {}
    if body.name is not None and body.name.strip():
        updates["name"] = body.name.strip()
    if body.intake_phone is not None:
        updates["intake_phone_e164"] = body.intake_phone.strip() or None
    if not updates:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing to update")
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    await db.execute(text(f"UPDATE organizations SET {set_clause}"), updates)
    return {"updated": list(updates.keys())}
