"""Shared fact model + derived aggregates for the lead-intelligence engines
(scoring, qualification, settlement). Pure data — constructable in tests without
a DB, and loadable from the persisted child tables.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SEV_RANK = {"Minor": 1, "Moderate": 2, "Severe": 3, "Permanent": 4}


def _num(x) -> float:
    try:
        return float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Facts:
    case_type: str = "Other Personal Injury"
    has_attorney: bool | None = None
    ai_summary: str | None = None
    incident_date: date | None = None
    police_report_available: bool | None = None
    fault_narrative: str | None = None
    comparative_negligence_pct: int | None = None
    injuries: list[dict] = field(default_factory=list)
    treatments: list[dict] = field(default_factory=list)
    policies: list[dict] = field(default_factory=list)
    parties: list[dict] = field(default_factory=list)
    damages: list[dict] = field(default_factory=list)


def derive(f: Facts) -> dict:
    """All shared aggregates the engines read (computed once)."""
    ranks: list[int] = []
    for inj in f.injuries:
        r = SEV_RANK.get(inj.get("severity"), 0)
        if r == 0 and (inj.get("is_permanent") or inj.get("requires_surgery")):
            r = 2  # severity-null but serious -> treat as Moderate
        ranks.append(r)
    max_sev = max(ranks) if ranks else 0
    any_permanent = any(i.get("is_permanent") or i.get("severity") == "Permanent" for i in f.injuries)
    any_surgery = any(i.get("requires_surgery") for i in f.injuries)

    def dsum(cat: str) -> float:
        return sum(_num(d.get("amount")) for d in f.damages if d.get("category") == cat)

    med_dmg = dsum("medical")
    future_med = dsum("future_medical")
    lost_wages = dsum("lost_wages")
    lost_ec = dsum("lost_earning_capacity")
    property_d = dsum("property")
    other_d = dsum("other")
    billed = sum(_num(t.get("billed_amount")) for t in f.treatments if _num(t.get("billed_amount")) > 0)
    reconciled_medical = max(med_dmg, billed)
    eligible_specials = reconciled_medical + lost_wages + 0.75 * future_med + 0.50 * lost_ec
    total_damages = sum(_num(d.get("amount")) for d in f.damages)
    distinct_providers = len({(t.get("provider_name") or "").strip().lower()
                              for t in f.treatments if (t.get("provider_name") or "").strip()})
    any_ongoing = any(t.get("is_ongoing") for t in f.treatments)
    treatment_backbone = len(f.treatments) > 0 or billed > 0 or med_dmg > 0

    def lim(p: dict) -> float:
        return _num(p.get("coverage_limit"))

    at_fault_liability = sum(lim(p) for p in f.policies
                             if p.get("policy_kind") == "Liability" and p.get("party_role") == "at_fault")
    um = [lim(p) for p in f.policies if p.get("policy_kind") == "UM" and p.get("party_role") == "claimant"]
    uim = [lim(p) for p in f.policies if p.get("policy_kind") == "UIM" and p.get("party_role") == "claimant"]
    um_cap = max(um) if um else 0.0
    uim_cap = max(uim) if uim else 0.0
    medpay = sum(lim(p) for p in f.policies if p.get("policy_kind") == "MedPay")
    relevant = [p for p in f.policies if p.get("policy_kind") not in ("Health", "Other")]
    coverage_known = any(p.get("coverage_limit") is not None or p.get("carrier_name") for p in relevant)
    available_coverage = (um_cap if at_fault_liability == 0 else at_fault_liability + max(0.0, uim_cap)) + medpay

    at_fault_party = any(p.get("role") == "at_fault" and p.get("full_name") for p in f.parties) \
        or any(p.get("party_role") == "at_fault" for p in f.policies)
    liability_signal = bool(f.police_report_available) or at_fault_party

    return {
        "max_sev": max_sev, "any_permanent": any_permanent, "any_surgery": any_surgery,
        "n_injuries": len(f.injuries),
        "reconciled_medical": reconciled_medical, "future_medical": future_med,
        "lost_wages": lost_wages, "lost_earning_capacity": lost_ec,
        "property": property_d, "other_economic": other_d,
        "billed": billed, "eligible_specials": eligible_specials, "total_damages": total_damages,
        "distinct_providers": distinct_providers, "any_ongoing": any_ongoing,
        "treatment_backbone": treatment_backbone, "n_treatments": len(f.treatments),
        "at_fault_liability": at_fault_liability, "um_cap": um_cap, "uim_cap": uim_cap,
        "medpay": medpay, "coverage_known": coverage_known, "available_coverage": available_coverage,
        "at_fault_party": at_fault_party, "liability_signal": liability_signal,
    }


def has_attorney_flag(f: Facts) -> bool:
    if f.has_attorney is True:
        return True
    return "already represented: yes" in (f.ai_summary or "").lower()


def sol_signal(incident_date: date | None, today: date, case_type: str) -> str:
    """Shared statute-of-limitations aging signal (single source for scoring +
    qualification). Never a legal determination — just an aging band."""
    if incident_date is None:
        return "unknown"
    age = (today - incident_date).days
    if age < 0:
        return "unknown"
    if case_type == "Wrongful Death":
        return "fresh" if age < 730 else "old"
    if age < 730:
        return "fresh"
    if age < 1095:
        return "aging"
    return "old"


async def load_facts(db: AsyncSession, lead_id: uuid.UUID) -> Facts:
    """Assemble Facts from the persisted lead + child tables (RLS-scoped session)."""
    lead = (await db.execute(
        text("SELECT case_type, ai_summary FROM leads WHERE id = :id"), {"id": lead_id}
    )).first()
    inc = (await db.execute(
        text("SELECT incident_date, police_report_available, fault_narrative, comparative_negligence_pct "
             "FROM incidents WHERE lead_id = :id ORDER BY created_at LIMIT 1"), {"id": lead_id}
    )).first()
    injuries = [dict(r._mapping) for r in (await db.execute(
        text("SELECT severity, is_permanent, requires_surgery FROM injuries WHERE lead_id=:id"),
        {"id": lead_id})).all()]
    treatments = [dict(r._mapping) for r in (await db.execute(
        text("SELECT provider_name, billed_amount, is_ongoing, start_date FROM medical_treatments WHERE lead_id=:id"),
        {"id": lead_id})).all()]
    policies = [dict(r._mapping) for r in (await db.execute(
        text("SELECT party_role, policy_kind, coverage_limit, carrier_name FROM insurance_policies WHERE lead_id=:id"),
        {"id": lead_id})).all()]
    parties = [dict(r._mapping) for r in (await db.execute(
        text("SELECT role, full_name FROM parties WHERE lead_id=:id"), {"id": lead_id})).all()]
    damages = [dict(r._mapping) for r in (await db.execute(
        text("SELECT category, amount, is_estimated FROM damages WHERE lead_id=:id"), {"id": lead_id})).all()]

    return Facts(
        case_type=(lead.case_type if lead else "Other Personal Injury"),
        ai_summary=(lead.ai_summary if lead else None),
        incident_date=(inc.incident_date if inc else None),
        police_report_available=(inc.police_report_available if inc else None),
        fault_narrative=(inc.fault_narrative if inc else None),
        comparative_negligence_pct=(inc.comparative_negligence_pct if inc else None),
        injuries=injuries, treatments=treatments, policies=policies,
        parties=parties, damages=damages,
    )
