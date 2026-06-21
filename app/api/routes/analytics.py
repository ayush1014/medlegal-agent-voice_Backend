"""Firm analytics — one aggregate endpoint powering the dashboard charts.

RLS scopes every query to the caller's firm (staff only).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_staff_db

router = APIRouter(prefix="/analytics", tags=["analytics"])


async def _pairs(db: AsyncSession, sql: str) -> list[dict]:
    return [{"key": r[0], "count": int(r[1])} for r in (await db.execute(text(sql))).all()]


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_staff_db)) -> dict:
    live = "FROM leads WHERE deleted_at IS NULL"

    totals = (await db.execute(text(
        f"SELECT count(*) AS leads, "
        f"count(*) FILTER (WHERE qualification_status='Qualified') AS qualified, "
        f"count(*) FILTER (WHERE lead_temperature='Hot') AS hot, "
        f"count(*) FILTER (WHERE retainer_status='Signed') AS signed, "
        f"coalesce(round(avg(lead_score)),0) AS avg_score {live}"
    ))).first()

    settlement = (await db.execute(text(
        f"SELECT coalesce(sum(settlement_expected),0) AS pipeline_value, "
        f"coalesce(round(avg(settlement_expected) FILTER (WHERE qualification_status='Qualified')),0) AS avg_qualified, "
        f"coalesce(sum(settlement_expected) FILTER (WHERE retainer_status='Signed'),0) AS signed_value {live}"
    ))).first()

    by_case_type = [
        {"key": r[0], "count": int(r[1]), "value": float(r[2] or 0)}
        for r in (await db.execute(text(
            f"SELECT case_type, count(*), coalesce(sum(settlement_expected),0) {live} "
            f"GROUP BY case_type ORDER BY count(*) DESC"
        ))).all()
    ]

    funnel = (await db.execute(text(
        "SELECT (SELECT count(*) FROM leads WHERE deleted_at IS NULL) AS total, "
        "(SELECT count(*) FROM leads WHERE deleted_at IS NULL AND qualification_status IN ('Qualified','Possibly Qualified')) AS qualified, "
        "(SELECT count(DISTINCT lead_id) FROM document_requests) AS docs_requested, "
        "(SELECT count(*) FROM leads WHERE deleted_at IS NULL AND retainer_status IN ('Sent','Viewed','Signed')) AS retainer_sent, "
        "(SELECT count(*) FROM leads WHERE deleted_at IS NULL AND retainer_status='Signed') AS signed"
    ))).first()

    over_time = [
        {"week": r[0].date().isoformat(), "count": int(r[1])}
        for r in (await db.execute(text(
            f"SELECT date_trunc('week', created_at) AS wk, count(*) {live} "
            f"AND created_at > now() - interval '8 weeks' GROUP BY wk ORDER BY wk"
        ))).all()
    ]

    return {
        "totals": {
            "leads": int(totals.leads), "qualified": int(totals.qualified),
            "hot": int(totals.hot), "signed": int(totals.signed),
            "avg_score": int(totals.avg_score),
        },
        "settlement": {
            "pipeline_value": float(settlement.pipeline_value or 0),
            "avg_qualified": float(settlement.avg_qualified or 0),
            "signed_value": float(settlement.signed_value or 0),
        },
        "by_pipeline": await _pairs(db, f"SELECT pipeline_status, count(*) {live} GROUP BY pipeline_status ORDER BY count(*) DESC"),
        "by_qualification": await _pairs(db, f"SELECT qualification_status, count(*) {live} GROUP BY qualification_status ORDER BY count(*) DESC"),
        "by_temperature": await _pairs(db, f"SELECT lead_temperature, count(*) {live} GROUP BY lead_temperature ORDER BY count(*) DESC"),
        "by_case_type": by_case_type,
        "funnel": [
            {"stage": "Total leads", "count": int(funnel.total)},
            {"stage": "Qualified", "count": int(funnel.qualified)},
            {"stage": "Docs requested", "count": int(funnel.docs_requested)},
            {"stage": "Retainer sent", "count": int(funnel.retainer_sent)},
            {"stage": "Signed", "count": int(funnel.signed)},
        ],
        "over_time": over_time,
    }
