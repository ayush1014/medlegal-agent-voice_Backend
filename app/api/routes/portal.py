"""Client portal API — a patient sees only their own case (RLS), can upload
documents, and can sign their retainer. All client-authed."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_client_db, require_csrf
from app.database import session_scope
from app.security.context import system_context
from app.services import document_service, retainer_service

router = APIRouter(prefix="/portal", tags=["portal"])


async def _my_lead(db: AsyncSession):
    """The caller's own lead (RLS scopes a client to their lead)."""
    # Client-safe fields only — never expose internal scoring/qualification/settlement value.
    return (await db.execute(
        text("SELECT id, organization_id, full_name, phone, email, case_type, pipeline_status, "
             "retainer_status, missing_documents, ai_summary, updated_at "
             "FROM leads WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 1")
    )).first()


def _rows(res) -> list[dict]:
    return [dict(r._mapping) for r in res.all()]


@router.get("/case")
async def my_case(db: AsyncSession = Depends(get_client_db)) -> dict:
    lead = await _my_lead(db)
    if lead is None:
        return {"lead": None, "document_requests": [], "documents": [], "retainer": None}
    org, lead_id = lead.organization_id, lead.id
    # Load children under system context scoped to the caller's own lead.
    async with session_scope(system_context(org)) as sdb:
        reqs = _rows(await sdb.execute(
            text("SELECT document_type, status, due_date FROM document_requests WHERE lead_id=:l ORDER BY created_at"),
            {"l": lead_id}))
        docs = _rows(await sdb.execute(
            text("SELECT file_name, mime_type, uploaded_by, scan_status, created_at FROM documents "
                 "WHERE lead_id=:l AND deleted_at IS NULL ORDER BY created_at DESC"), {"l": lead_id}))
        ret = (await sdb.execute(
            text("SELECT status, sent_at, viewed_at, signed_at FROM retainers WHERE lead_id=:l "
                 "AND deleted_at IS NULL LIMIT 1"), {"l": lead_id})).first()
        inc = (await sdb.execute(
            text("SELECT incident_date, location_text, description FROM incidents WHERE lead_id=:l "
                 "ORDER BY created_at LIMIT 1"), {"l": lead_id})).first()
        injuries = _rows(await sdb.execute(
            text("SELECT body_part, severity, is_permanent, requires_surgery FROM injuries "
                 "WHERE lead_id=:l ORDER BY created_at"), {"l": lead_id}))
    return {
        "lead": dict(lead._mapping),
        "incident": dict(inc._mapping) if inc else None,
        "injuries": injuries,
        "document_requests": reqs,
        "documents": docs,
        "retainer": dict(ret._mapping) if ret else None,
    }


@router.post("/documents/upload", dependencies=[Depends(require_csrf)])
async def upload(file: UploadFile = File(...), db: AsyncSession = Depends(get_client_db)) -> dict:
    lead = await _my_lead(db)
    if lead is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No case on file")
    content = await file.read()
    doc_id = await document_service.record_upload(
        lead.organization_id, lead.id, file_name=file.filename or "upload",
        content=content, mime=file.content_type, uploaded_by="client",
    )
    return {"document_id": str(doc_id), "status": "received"}


@router.post("/retainer/sign", dependencies=[Depends(require_csrf)])
async def sign(db: AsyncSession = Depends(get_client_db)) -> dict:
    lead = await _my_lead(db)
    if lead is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No case on file")
    org, lead_id = lead.organization_id, lead.id
    async with session_scope(system_context(org)) as sdb:
        rid = (await sdb.execute(
            text("SELECT id FROM retainers WHERE lead_id=:l AND deleted_at IS NULL LIMIT 1"),
            {"l": lead_id})).scalar_one_or_none()
    if rid is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No retainer to sign")
    # Same path as the emailed magic-link: records signed, generates the PDF, emails a copy.
    return await retainer_service.finalize_sign(org, lead_id, rid, lead.full_name)
