"""Lead intelligence orchestrator: qualify -> score -> estimate settlement, then
persist (lead_scores history + leads headline + settlement_estimates history).

Runs from the `intake.completed` outbox handler (post-call) and from the manual
rescore endpoint. The settlement LLM nudge is opt-in (use_llm) to keep the funnel
deterministic and free of per-lead network cost by default.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import (
    outbox_publisher,
    qualification_service,
    scoring_service,
    settlement_service,
)
from app.services.lead_facts import load_facts

# Qualification status -> pipeline stage.
_PIPELINE = {"Qualified": "Qualified", "Unqualified": "Rejected"}


def _dec(v) -> Decimal | None:
    return Decimal(str(round(float(v), 2))) if v is not None else None


async def run_for_lead(db: AsyncSession, lead_id: uuid.UUID, *, use_llm: bool = False) -> dict:
    org = (await db.execute(
        text("SELECT organization_id FROM leads WHERE id = :id"), {"id": lead_id}
    )).scalar_one_or_none()
    if org is None:
        raise ValueError("lead not found")

    facts = await load_facts(db, lead_id)
    q = qualification_service.qualify(facts)
    sc = scoring_service.score(facts)
    st = settlement_service.estimate(
        facts, hard_block=q["hard_block"], qual_reason=q["reason"], use_llm=use_llm
    )

    await db.execute(
        text("INSERT INTO lead_scores (organization_id, lead_id, score, temperature, "
             "qualification_status, qualification_reason, reasoning, model, created_by_type) "
             "VALUES (:o,:l,:s,:t,:qs,:qr, CAST(:rj AS jsonb), :m, 'system')"),
        {"o": org, "l": lead_id, "s": sc["score"], "t": sc["temperature"],
         "qs": q["status"], "qr": q["reason"], "rj": json.dumps(sc["reasoning"]), "m": sc["model"]},
    )
    await db.execute(
        text("UPDATE leads SET lead_score=:s, lead_temperature=:t, qualification_status=:qs, "
             "settlement_expected=:se, pipeline_status=:ps WHERE id=:id"),
        {"s": sc["score"], "t": sc["temperature"], "qs": q["status"],
         "se": _dec(st["expected"]), "ps": _PIPELINE.get(q["status"], "Needs Review"), "id": lead_id},
    )
    await db.execute(
        text("INSERT INTO settlement_estimates (organization_id, lead_id, low, expected, high, "
             "confidence, pain_multiplier, inputs_snapshot, model, reasoning, created_by_type) "
             "VALUES (:o,:l,:lo,:ex,:hi,:cf,:pm, CAST(:snap AS jsonb), :m, :rs, 'system')"),
        {"o": org, "l": lead_id, "lo": _dec(st["low"]), "ex": _dec(st["expected"]),
         "hi": _dec(st["high"]), "cf": st["confidence"], "pm": _dec(st["pain_multiplier"]),
         "snap": json.dumps(st["inputs_snapshot"]), "m": st["model"], "rs": st["reasoning"]},
    )

    return {
        "score": sc["score"], "score_raw": sc["score_raw"], "temperature": sc["temperature"],
        "qualification": q["status"], "qualification_reason": q["reason"],
        "settlement": {"low": st["low"], "expected": st["expected"], "high": st["high"],
                       "confidence": st["confidence"]},
    }


@outbox_publisher.on("intake.completed")
async def _on_intake_completed(db, organization_id, aggregate_id, payload) -> None:
    """Post-call: score + qualify + estimate the freshly-completed lead."""
    if aggregate_id is not None:
        await run_for_lead(db, aggregate_id)
