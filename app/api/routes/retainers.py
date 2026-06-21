"""Retainer / LOR endpoints: staff send the agreement; client signs via token."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_staff_db, require_csrf
from app.services import retainer_service

router = APIRouter(prefix="/retainers", tags=["retainers"])


class RetainerSendIn(BaseModel):
    lead_id: uuid.UUID


@router.post("/send", dependencies=[Depends(require_csrf)])
async def send_retainer(body: RetainerSendIn, db: AsyncSession = Depends(get_staff_db)) -> dict:
    org = (await db.execute(
        text("SELECT organization_id FROM leads WHERE id=:l AND deleted_at IS NULL"),
        {"l": body.lead_id})).scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return await retainer_service.prepare_and_send(org, body.lead_id)


@router.post("/sign")
async def sign(request: Request, code: str = Form(...)) -> dict:
    """Client e-sign via short-code link (internal mock). No login."""
    try:
        return await retainer_service.sign_with_code(
            code, ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    except Exception:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired link")
