"""Lead qualification — deterministic ordered decision rules (rubric v2).

Returns the status (Qualified / Possibly Qualified / Needs Review / Unqualified),
a human reason, and hard_block (which suppresses the settlement estimate).
"""

from __future__ import annotations

from datetime import date

from app.services.lead_facts import Facts, derive, has_attorney_flag, sol_signal

_DEATH_WORDS = ("death", "died", "passed away", "deceased", "killed", "fatal")


def _death_corroborated(f: Facts, d: dict) -> bool:
    text = ((f.ai_summary or "") + " " + (f.fault_narrative or "")).lower()
    return (
        any(w in text for w in _DEATH_WORDS)
        or d["total_damages"] > 0
        or bool(f.police_report_available) or d["at_fault_party"]
        or f.comparative_negligence_pct is not None or bool(f.fault_narrative)
    )


def _liability_points(f: Facts, d: dict) -> int:
    pts = 0
    if f.police_report_available:
        pts += 2
    if d["at_fault_party"]:
        pts += 2
    if f.comparative_negligence_pct is not None and f.comparative_negligence_pct <= 25:
        pts += 1
    if f.fault_narrative:
        pts += 1
    if f.comparative_negligence_pct is not None and f.comparative_negligence_pct >= 50:
        pts -= 3
    return pts


def qualify(f: Facts, today: date | None = None) -> dict:
    today = today or date.today()
    d = derive(f)
    sol = sol_signal(f.incident_date, today, f.case_type)
    comp = f.comparative_negligence_pct
    is_wd = f.case_type == "Wrongful Death"

    def out(status: str, reason: str, hard_block: bool) -> dict:
        return {"status": status, "reason": reason, "hard_block": hard_block, "sol_signal": sol}

    # 1. Already represented -> conflict.
    if has_attorney_flag(f):
        return out("Needs Review",
                   "Caller appears to already be represented by another attorney — taking this "
                   "case would be a conflict; route to conflict-check before any contact.", True)

    # 2. Wrongful-death carve-out.
    wd_corroborated = False
    if is_wd:
        if _death_corroborated(f, d):
            wd_corroborated = True  # skip injury/treatment disqualifiers, fall through
        elif d["total_damages"] == 0 and not d["treatment_backbone"]:
            return out("Needs Review",
                       "Wrongful-death matter with no documented damages or corroborating detail yet "
                       "— needs human review; never auto-reject a death case.", False)

    # 3-5. Injury / treatment disqualifiers (skipped for corroborated wrongful death).
    if not wd_corroborated:
        if d["n_injuries"] == 0 and not d["treatment_backbone"] and d["total_damages"] == 0 \
                and f.case_type == "Other Personal Injury":
            return out("Unqualified",
                       "No injury, no medical treatment, and no damages were reported, and the matter "
                       "is uncategorized — this does not present as a viable personal-injury claim.", True)
        if d["n_injuries"] == 0:
            return out("Unqualified",
                       "No injuries were reported. A personal-injury claim requires a compensable "
                       "bodily injury; without one the case is not viable.", True)
        if not d["treatment_backbone"]:
            if f.incident_date is not None and (today - f.incident_date).days <= 30:
                return out("Needs Review",
                           "An injury was reported but no treatment is on record yet — recent incident, "
                           "confirm care before grading.", False)
            return out("Unqualified",
                       "An injury was reported but there is no medical treatment, billing, or medical "
                       "damages on record — treatment is the damages backbone of a PI case.", True)

    # 6. SOL signal old.
    if sol == "old":
        return out("Needs Review",
                   "The incident appears to be over the typical filing window for this case type. This "
                   "may affect the statute of limitations and must be confirmed with an attorney — not "
                   "auto-rejected on date alone.", False)

    # 7. Incident date missing.
    if f.incident_date is None:
        return out("Needs Review",
                   "The incident date is missing, so the statute of limitations cannot be assessed — "
                   "confirm the date before qualifying.", False)

    # 8-10. Scored resolution. A "qualifying" injury is meaningful (Moderate+),
    # or any surgery/permanency — minor-only falls to Possibly Qualified (rule 9).
    liability_clear = _liability_points(f, d) >= 3
    has_real_injury = d["n_injuries"] > 0 and (d["max_sev"] >= 2 or d["any_permanent"] or d["any_surgery"])
    comp_ok = comp is None or comp < 50

    if liability_clear and has_real_injury and d["treatment_backbone"] and comp_ok and sol == "fresh":
        return out("Qualified",
                   "Clear liability with a documented injury and treatment. This presents as a viable "
                   "personal-injury claim ready to advance.", False)
    if liability_clear and has_real_injury and d["treatment_backbone"] and comp_ok and sol == "aging":
        return out("Possibly Qualified",
                   "Strong on the merits, but the incident is aging toward the filing window — confirm "
                   "the statute of limitations before advancing.", False)

    liability_unclear = not f.police_report_available and not d["at_fault_party"] and comp is None
    minor_only = d["max_sev"] <= 1 and not d["any_surgery"] and not d["any_permanent"]
    confirmed_no_coverage = d["coverage_known"] and d["available_coverage"] <= 0
    if liability_unclear or (comp is not None and 25 < comp < 50) or confirmed_no_coverage or minor_only:
        if liability_unclear:
            reason = ("Injury and treatment are present, but liability is not yet clear (no police "
                      "report, at-fault party, or fault detail) — promising but needs fault confirmation.")
        elif confirmed_no_coverage:
            reason = ("Injury and treatment are present, but no recoverable insurance coverage was "
                      "identified — viable only if a source of recovery can be found.")
        elif minor_only:
            reason = ("Liability looks workable but the injury appears minor with no surgery or "
                      "permanency — value is likely modest.")
        else:
            reason = "Liability is contested (significant comparative fault) — recovery may be reduced."
        return out("Possibly Qualified", reason, False)

    return out("Needs Review",
               "The case has an injury and treatment but the available facts are too thin or mixed to "
               "grade automatically — a human should review.", False)
