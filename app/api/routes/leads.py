"""Lead management API for the firm dashboard.

RLS scopes every query automatically: staff see the whole firm, a client sees only
their own lead. Management mutations + the full detail aggregate require staff.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_authed_db, get_staff_db, require_csrf
from app.models.enums import (
    LEAD_TEMPERATURES,
    PIPELINE_STATUSES,
    QUALIFICATION_STATUSES,
)
from app.schemas.lead import LeadOut

router = APIRouter(prefix="/leads", tags=["leads"])

_SELECT = """
    SELECT l.id, l.full_name, l.phone, l.email, l.case_type,
           l.qualification_status, l.lead_score, l.lead_temperature,
           l.settlement_expected, l.pipeline_status, l.retainer_status,
           l.missing_documents, l.ai_summary, l.updated_at,
           (SELECT i.incident_date FROM incidents i
              WHERE i.lead_id = l.id ORDER BY i.created_at LIMIT 1) AS incident_date
    FROM leads l
    WHERE l.deleted_at IS NULL
"""

_SORTS = {
    "score": "l.lead_score DESC",
    "updated": "l.updated_at DESC",
    "value": "l.settlement_expected DESC NULLS LAST",
    "created": "l.created_at DESC",
}


def _to_out(row) -> LeadOut:
    m = row._mapping
    return LeadOut(
        id=str(m["id"]), full_name=m["full_name"], phone=m["phone"], email=m["email"],
        case_type=m["case_type"], incident_date=m["incident_date"],
        qualification_status=m["qualification_status"], lead_score=m["lead_score"],
        lead_temperature=m["lead_temperature"], settlement_expected=m["settlement_expected"],
        pipeline_status=m["pipeline_status"], retainer_status=m["retainer_status"],
        missing_documents=m["missing_documents"], ai_summary=m["ai_summary"],
        updated_at=m["updated_at"],
    )


async def _rows(db: AsyncSession, sql: str, params: dict | None = None) -> list[dict]:
    res = await db.execute(text(sql), params or {})
    return [dict(r._mapping) for r in res.all()]


@router.get("", response_model=list[LeadOut])
async def list_leads(
    db: AsyncSession = Depends(get_authed_db),
    q: str | None = Query(None, description="search name/phone/email"),
    pipeline_status: str | None = None,
    qualification_status: str | None = None,
    temperature: str | None = None,
    sort: str = Query("updated", pattern="^(score|updated|value|created)$"),
    limit: int = Query(100, le=500),
    offset: int = 0,
) -> list[LeadOut]:
    sql = _SELECT
    params: dict = {}
    if q:
        sql += " AND (l.full_name ILIKE :q OR l.phone ILIKE :q OR l.email ILIKE :q)"
        params["q"] = f"%{q}%"
    if pipeline_status:
        sql += " AND l.pipeline_status = :ps"
        params["ps"] = pipeline_status
    if qualification_status:
        sql += " AND l.qualification_status = :qs"
        params["qs"] = qualification_status
    if temperature:
        sql += " AND l.lead_temperature = :temp"
        params["temp"] = temperature
    sql += f" ORDER BY {_SORTS[sort]} LIMIT :limit OFFSET :offset"
    params.update(limit=limit, offset=offset)
    rows = (await db.execute(text(sql), params)).all()
    return [_to_out(r) for r in rows]


@router.get("/{lead_id}", response_model=LeadOut)
async def get_lead(lead_id: uuid.UUID, db: AsyncSession = Depends(get_authed_db)) -> LeadOut:
    row = (await db.execute(text(_SELECT + " AND l.id = :id"), {"id": lead_id})).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return _to_out(row)


@router.get("/{lead_id}/detail")
async def lead_detail(lead_id: uuid.UUID, db: AsyncSession = Depends(get_staff_db)) -> dict:
    """The full case file: headline + all child facts + intelligence + funnel state."""
    head = (await db.execute(text(_SELECT + " AND l.id = :id"), {"id": lead_id})).first()
    if head is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    p = {"id": lead_id}
    lead = dict(head._mapping)
    # Profile fields the list query omits but the case file should show.
    prof = (await db.execute(text(
        "SELECT date_of_birth, address, occupation, employer, employment_status, annual_income, "
        "preferred_contact_method, best_time_to_contact, case_brief FROM leads WHERE id=:id"), p)).first()
    if prof is not None:
        lead.update(dict(prof._mapping))
    return {
        "lead": lead,
        # The full intake conversation, so staff can read everything the agent gathered.
        "transcript": (await _rows(db, "SELECT full_text, status, created_at FROM intake_transcripts "
                                   "WHERE lead_id=:id AND full_text IS NOT NULL "
                                   "ORDER BY length(full_text) DESC LIMIT 1", p) or [None])[0],
        "incidents": await _rows(db, "SELECT * FROM incidents WHERE lead_id=:id ORDER BY created_at", p),
        "injuries": await _rows(db, "SELECT * FROM injuries WHERE lead_id=:id ORDER BY created_at", p),
        "treatments": await _rows(db, "SELECT * FROM medical_treatments WHERE lead_id=:id ORDER BY created_at", p),
        "insurance_policies": await _rows(db, "SELECT * FROM insurance_policies WHERE lead_id=:id ORDER BY created_at", p),
        "parties": await _rows(db, "SELECT * FROM parties WHERE lead_id=:id ORDER BY created_at", p),
        "damages": await _rows(db, "SELECT * FROM damages WHERE lead_id=:id ORDER BY created_at", p),
        "score": (await _rows(db, "SELECT * FROM lead_scores WHERE lead_id=:id ORDER BY created_at DESC LIMIT 1", p) or [None])[0],
        "settlement": (await _rows(db, "SELECT * FROM settlement_estimates WHERE lead_id=:id ORDER BY created_at DESC LIMIT 1", p) or [None])[0],
        "document_requests": await _rows(db, "SELECT * FROM document_requests WHERE lead_id=:id ORDER BY created_at", p),
        "documents": await _rows(db, "SELECT * FROM documents WHERE lead_id=:id AND deleted_at IS NULL ORDER BY created_at", p),
        "retainer": (await _rows(db, "SELECT * FROM retainers WHERE lead_id=:id AND deleted_at IS NULL LIMIT 1", p) or [None])[0],
        "tasks": await _rows(db, "SELECT * FROM tasks WHERE lead_id=:id ORDER BY due_at NULLS LAST, created_at", p),
        "messages": await _rows(db, "SELECT id, channel, direction, body, purpose, status, created_at FROM messages WHERE lead_id=:id ORDER BY created_at DESC LIMIT 30", p),
        "timeline": await _rows(db, "SELECT event_type, name, payload, created_at FROM agent_events WHERE lead_id=:id ORDER BY created_at DESC LIMIT 40", p),
    }


@router.get("/{lead_id}/documents/{doc_id}/file")
async def lead_document_file(
    lead_id: uuid.UUID, doc_id: uuid.UUID, db: AsyncSession = Depends(get_staff_db)
) -> Response:
    """Stream a document inline (for in-UI preview/open). RLS-scoped: the file must
    belong to this lead in the staff's org. We proxy the bytes (no public signed URL)
    so access stays behind auth."""
    from app.services import document_service

    row = (await db.execute(
        text("SELECT storage_url, mime_type, file_name FROM documents "
             "WHERE id=:d AND lead_id=:l AND deleted_at IS NULL"),
        {"d": doc_id, "l": lead_id})).first()
    if row is None or not row.storage_url:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    content = await asyncio.to_thread(document_service.load_object, row.storage_url)
    return Response(
        content=content, media_type=row.mime_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{row.file_name or "document"}"'},
    )


class LeadPatch(BaseModel):
    pipeline_status: str | None = None
    qualification_status: str | None = None
    lead_temperature: str | None = None
    assigned_user_id: uuid.UUID | None = None


@router.patch("/{lead_id}", response_model=LeadOut, dependencies=[Depends(require_csrf)])
async def update_lead(
    lead_id: uuid.UUID, body: LeadPatch, db: AsyncSession = Depends(get_staff_db)
) -> LeadOut:
    updates: dict = {}
    if body.pipeline_status is not None:
        if body.pipeline_status not in PIPELINE_STATUSES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid pipeline_status")
        updates["pipeline_status"] = body.pipeline_status
    if body.qualification_status is not None:
        if body.qualification_status not in QUALIFICATION_STATUSES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid qualification_status")
        updates["qualification_status"] = body.qualification_status
    if body.lead_temperature is not None:
        if body.lead_temperature not in LEAD_TEMPERATURES:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid lead_temperature")
        updates["lead_temperature"] = body.lead_temperature
    if body.assigned_user_id is not None:
        updates["assigned_user_id"] = body.assigned_user_id
    if updates:
        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        res = await db.execute(
            text(f"UPDATE leads SET {set_clause} WHERE id = :id AND deleted_at IS NULL"),
            {**updates, "id": lead_id},
        )
        if res.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    row = (await db.execute(text(_SELECT + " AND l.id = :id"), {"id": lead_id})).first()
    return _to_out(row)


@router.post("/{lead_id}/rescore", dependencies=[Depends(require_csrf)])
async def rescore_lead(lead_id: uuid.UUID, db: AsyncSession = Depends(get_staff_db)) -> dict:
    """Re-run scoring + qualification + settlement for a lead (manual trigger)."""
    from app.services import lead_intelligence

    exists = (await db.execute(
        text("SELECT 1 FROM leads WHERE id=:id AND deleted_at IS NULL"), {"id": lead_id}
    )).first()
    if exists is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return await lead_intelligence.run_for_lead(db, lead_id)
