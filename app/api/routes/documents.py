"""Document gathering endpoints.

- Staff trigger a document request for a lead (SMS/WhatsApp with a short link).
- Clients upload via the short-code link (no login) — see routes/links.py for the page.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_staff_db, require_csrf
from app.services import document_service, short_links

router = APIRouter(prefix="/documents", tags=["documents"])


class DocRequestIn(BaseModel):
    lead_id: uuid.UUID
    document_types: list[str] | None = None


@router.post("/request", dependencies=[Depends(require_csrf)])
async def request_docs(body: DocRequestIn, db: AsyncSession = Depends(get_staff_db)) -> dict:
    org = (await db.execute(
        text("SELECT organization_id FROM leads WHERE id=:l AND deleted_at IS NULL"),
        {"l": body.lead_id})).scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return await document_service.request_documents(org, body.lead_id, doc_types=body.document_types)


@router.post("/upload")
async def upload(code: str = Form(...), file: UploadFile = File(...)) -> dict:
    """Client upload via short-code link (no login). Code scopes org + lead."""
    resolved = await short_links.resolve(code)
    if resolved is None or resolved["purpose"] != short_links.UPLOAD:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired link")
    content = await file.read()
    doc_id = await document_service.record_upload(
        resolved["organization_id"], resolved["lead_id"],
        file_name=file.filename or "upload", content=content,
        mime=file.content_type, uploaded_by="client",
    )
    return {"document_id": str(doc_id), "status": "received"}
