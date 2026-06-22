"""Persist a structured Extraction into the lead + child fact tables.

All enum values are normalized server-side (coerced to a valid member or null)
so malformed model output can never violate a CHECK constraint. Runs under the
firm's system context.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import IntakeContext
from app.agent.extraction import Extraction, extract_from_transcript
from app.database import session_scope
from app.models.enums import (
    CASE_TYPES,
    DAMAGE_CATEGORIES,
    INJURY_SEVERITIES,
    PARTY_ROLES,
    POLICY_KINDS,
    POLICY_PARTY_ROLES,
)
from app.security.context import system_context
from app.services import intake_service


def _date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def _money(v: float | None) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


async def _exists(db, sql: str, params: dict) -> bool:
    """Dedup guard — True if a matching child row already exists (returning callers)."""
    return (await db.execute(text(sql), params)).first() is not None


def _one_of(value: str | None, allowed: list[str], default: str | None) -> str | None:
    return value if value in allowed else default


async def persist_extraction(db: AsyncSession, ctx: IntakeContext, ex: Extraction) -> dict:
    org, lead = ctx.organization_id, ctx.lead_id

    # --- Lead headline upgrade ---
    fields: dict = {}
    if ex.lead.full_name and ex.lead.full_name.strip().lower() != "caller":
        fields["full_name"] = ex.lead.full_name.strip()
    if ex.lead.email:
        fields["email"] = ex.lead.email.strip().lower()
    if ex.lead.address:
        fields["address"] = ex.lead.address.strip()
    if ex.lead.occupation:
        fields["occupation"] = ex.lead.occupation.strip()
    if ex.lead.employer:
        fields["employer"] = ex.lead.employer.strip()
    if ex.lead.employment_status:
        fields["employment_status"] = ex.lead.employment_status.strip()
    if ex.lead.annual_income is not None:
        fields["annual_income"] = ex.lead.annual_income
    if ex.lead.case_type:
        fields["case_type"] = _one_of(ex.lead.case_type, CASE_TYPES, "Other Personal Injury")
    if ex.lead.preferred_contact_method:
        fields["preferred_contact_method"] = ex.lead.preferred_contact_method
    if ex.lead.best_time_to_contact:
        fields["best_time_to_contact"] = ex.lead.best_time_to_contact

    summary = (ex.lead.summary or "").strip()
    if ex.lead.has_attorney is not None:
        summary = (summary + f" (Already represented: {'yes' if ex.lead.has_attorney else 'no'}.)").strip()
    if summary:
        fields["ai_summary"] = summary

    await intake_service.update_partial_lead(db, ctx, fields)
    await db.execute(
        text("UPDATE leads SET pipeline_status = 'Intake Complete' WHERE id = :id"),
        {"id": lead},
    )
    dob = _date(ex.lead.date_of_birth)
    if dob is not None:
        await db.execute(
            text("UPDATE leads SET date_of_birth = :dob WHERE id = :id"),
            {"dob": dob, "id": lead},
        )

    counts = {"incidents": 0, "injuries": 0, "treatments": 0, "policies": 0, "parties": 0, "damages": 0}

    for inc in ex.incidents:
        d = _date(inc.incident_date)
        if d is not None and await _exists(db, "SELECT 1 FROM incidents WHERE lead_id=:l AND incident_date=:d",
                                           {"l": lead, "d": d}):
            continue
        if d is None and inc.description and await _exists(
                db, "SELECT 1 FROM incidents WHERE lead_id=:l AND description=:desc",
                {"l": lead, "desc": inc.description}):
            continue
        await db.execute(
            text("INSERT INTO incidents (organization_id, lead_id, incident_date, location_text, "
                 "description, police_report_available, fault_narrative, comparative_negligence_pct) "
                 "VALUES (:o,:l,:d,:loc,:desc,:pr,:fn,:cn)"),
            {"o": org, "l": lead, "d": d, "loc": inc.location_text,
             "desc": inc.description, "pr": inc.police_report_available, "fn": inc.fault_narrative,
             "cn": inc.comparative_negligence_pct},
        )
        counts["incidents"] += 1

    for inj in ex.injuries:
        if inj.body_part and await _exists(
                db, "SELECT 1 FROM injuries WHERE lead_id=:l AND lower(body_part)=lower(:bp)",
                {"l": lead, "bp": inj.body_part}):
            continue
        await db.execute(
            text("INSERT INTO injuries (organization_id, lead_id, body_part, description, severity, "
                 "is_permanent, requires_surgery) VALUES (:o,:l,:bp,:desc,:sev,:perm,:surg)"),
            {"o": org, "l": lead, "bp": inj.body_part, "desc": inj.description,
             "sev": _one_of(inj.severity, INJURY_SEVERITIES, None),
             "perm": inj.is_permanent, "surg": inj.requires_surgery},
        )
        counts["injuries"] += 1

    for t in ex.treatments:
        if t.provider_name and await _exists(
                db, "SELECT 1 FROM medical_treatments WHERE lead_id=:l AND lower(provider_name)=lower(:pn)",
                {"l": lead, "pn": t.provider_name}):
            continue
        await db.execute(
            text("INSERT INTO medical_treatments (organization_id, lead_id, provider_name, "
                 "provider_type, treatment_type, start_date, end_date, is_ongoing, billed_amount) "
                 "VALUES (:o,:l,:pn,:pt,:tt,:sd,:ed,:ong,:amt)"),
            {"o": org, "l": lead, "pn": t.provider_name, "pt": t.provider_type, "tt": t.treatment_type,
             "sd": _date(t.start_date), "ed": _date(t.end_date), "ong": t.is_ongoing,
             "amt": _money(t.billed_amount)},
        )
        counts["treatments"] += 1

    for p in ex.insurance_policies:
        if p.carrier_name and await _exists(
                db, "SELECT 1 FROM insurance_policies WHERE lead_id=:l AND lower(carrier_name)=lower(:cn) "
                    "AND coalesce(policy_kind,'')=coalesce(:pk,'')",
                {"l": lead, "cn": p.carrier_name, "pk": _one_of(p.policy_kind, POLICY_KINDS, None)}):
            continue
        await db.execute(
            text("INSERT INTO insurance_policies (organization_id, lead_id, party_role, carrier_name, "
                 "policy_kind, coverage_limit, claim_number) VALUES (:o,:l,:pr,:cn,:pk,:cl,:claim)"),
            {"o": org, "l": lead, "pr": _one_of(p.party_role, POLICY_PARTY_ROLES, "other"),
             "cn": p.carrier_name, "pk": _one_of(p.policy_kind, POLICY_KINDS, None),
             "cl": _money(p.coverage_limit), "claim": p.claim_number},
        )
        counts["policies"] += 1

    for pa in ex.parties:
        if pa.full_name and await _exists(
                db, "SELECT 1 FROM parties WHERE lead_id=:l AND lower(full_name)=lower(:name)",
                {"l": lead, "name": pa.full_name}):
            continue
        await db.execute(
            text("INSERT INTO parties (organization_id, lead_id, role, full_name, notes) "
                 "VALUES (:o,:l,:role,:name,:notes)"),
            {"o": org, "l": lead, "role": _one_of(pa.role, PARTY_ROLES, "other"),
             "name": pa.full_name, "notes": pa.notes},
        )
        counts["parties"] += 1

    for d in ex.damages:
        amount = _money(d.amount)
        if amount is None:  # damages.amount is NOT NULL
            continue
        cat = _one_of(d.category, DAMAGE_CATEGORIES, "other")
        if await _exists(db, "SELECT 1 FROM damages WHERE lead_id=:l AND category=:cat AND amount=:amt",
                         {"l": lead, "cat": cat, "amt": amount}):
            continue
        await db.execute(
            text("INSERT INTO damages (organization_id, lead_id, category, description, amount, is_estimated) "
                 "VALUES (:o,:l,:cat,:desc,:amt,:est)"),
            {"o": org, "l": lead, "cat": _one_of(d.category, DAMAGE_CATEGORIES, "other"),
             "desc": d.description, "amt": amount, "est": d.is_estimated},
        )
        counts["damages"] += 1

    await intake_service.log_event(
        db, ctx, event_type="tool_result", name="post_call_extraction", payload=counts
    )
    return counts


async def run_post_call_extraction(
    organization_id: uuid.UUID, lead_id: uuid.UUID, transcript_text: str
) -> dict:
    """Extract from the transcript and persist into the lead + child tables."""
    ex = await extract_from_transcript(transcript_text)
    ctx = IntakeContext(organization_id=organization_id, caller_phone="", lead_id=lead_id)
    async with session_scope(system_context(organization_id)) as db:
        return await persist_extraction(db, ctx, ex)
