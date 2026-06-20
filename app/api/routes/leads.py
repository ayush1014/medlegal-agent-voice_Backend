"""Lead read endpoints for the dashboard.

Minimal slice to satisfy PRD-1's "dashboards read real data" — the full lead
CRUD + AI endpoints come with the API-layer PRD. RLS scopes results
automatically: an admin sees the whole firm, a client sees only their own lead.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_authed_db
from app.schemas.lead import LeadOut

router = APIRouter(prefix="/leads", tags=["leads"])

# Headline fields + first incident date; RLS does the access scoping.
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


def _to_out(row) -> LeadOut:
    m = row._mapping
    return LeadOut(
        id=str(m["id"]),
        full_name=m["full_name"],
        phone=m["phone"],
        email=m["email"],
        case_type=m["case_type"],
        incident_date=m["incident_date"],
        qualification_status=m["qualification_status"],
        lead_score=m["lead_score"],
        lead_temperature=m["lead_temperature"],
        settlement_expected=m["settlement_expected"],
        pipeline_status=m["pipeline_status"],
        retainer_status=m["retainer_status"],
        missing_documents=m["missing_documents"],
        ai_summary=m["ai_summary"],
        updated_at=m["updated_at"],
    )


@router.get("", response_model=list[LeadOut])
async def list_leads(db: AsyncSession = Depends(get_authed_db)) -> list[LeadOut]:
    rows = (await db.execute(text(_SELECT + " ORDER BY l.updated_at DESC"))).all()
    return [_to_out(r) for r in rows]


@router.get("/{lead_id}", response_model=LeadOut)
async def get_lead(lead_id: uuid.UUID, db: AsyncSession = Depends(get_authed_db)) -> LeadOut:
    row = (await db.execute(text(_SELECT + " AND l.id = :id"), {"id": lead_id})).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return _to_out(row)
