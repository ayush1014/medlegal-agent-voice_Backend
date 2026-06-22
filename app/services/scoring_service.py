"""Lead scoring — deterministic 0-100 weighted-factor model (rubric v2).

Seven positive factors sum to exactly 100. Representation conflict is applied
AFTER summation (caps the final at 15) so a human-cleared lead can be re-ranked.
Temperature comes from the final score plus defensive guards that only push down.
"""

from __future__ import annotations

from datetime import date

from app.services.lead_facts import Facts, derive, has_attorney_flag

_HIGH_TIER = {"Truck Accident", "Wrongful Death", "Motorcycle Accident",
              "Pedestrian Accident", "Rideshare Accident"}
_CASE_PRIOR = {
    "Truck Accident": 8, "Wrongful Death": 8, "Motorcycle Accident": 7,
    "Pedestrian Accident": 7, "Rideshare Accident": 7, "Auto Accident": 6,
    "Premises Liability": 5, "Dog Bite": 5, "Slip and Fall": 4,
    "Workplace Injury": 4, "Other Personal Injury": 3,
}
_CORROBORATE_KW = {
    "Truck Accident": ("truck", "semi", "tractor", "trailer", "carrier", "commercial"),
    "Motorcycle Accident": ("motorcycle", "motorbike", "bike", "rider"),
    "Pedestrian Accident": ("pedestrian", "walking", "crosswalk", "on foot", "sidewalk"),
    "Rideshare Accident": ("uber", "lyft", "rideshare", "rideshare app"),
    "Wrongful Death": ("death", "died", "passed away", "deceased", "killed", "fatal"),
}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def score(f: Facts, today: date | None = None) -> dict:
    today = today or date.today()
    d = derive(f)
    reasoning: list[str] = []

    # 1. Liability strength (22)
    liab = 6.0
    if f.police_report_available:
        liab += 6
    if any(p.get("role") == "at_fault" and p.get("full_name") for p in f.parties):
        liab += 3
    if any(p.get("party_role") == "at_fault" for p in f.policies):
        liab += 3
    if f.fault_narrative and len(f.fault_narrative.strip()) >= 40:
        liab += 2
    comp = f.comparative_negligence_pct
    if comp == 0:
        liab += 2
    elif comp is not None and 26 <= comp <= 50:
        liab -= 4
    elif comp is not None and 51 <= comp <= 99:
        liab -= 8
    elif comp == 100:
        liab -= 12
    liab = _clamp(liab, 0, 22)
    reasoning.append(f"Liability strength: {liab:.0f}/22"
                     + ("" if comp is not None else " (comparative fault unverified)"))

    # 2. Injury severity (20)
    sev = {0: 0, 1: 6, 2: 11, 3: 16, 4: 20}[d["max_sev"]]
    if d["any_permanent"]:
        sev = max(sev, 18)
    if d["any_surgery"]:
        sev += 3
    if d["n_injuries"] == 0:
        sev = 0
    sev = _clamp(sev, 0, 20)
    reasoning.append(f"Injury severity: {sev:.0f}/20")

    # 3. Treatment depth (16)
    if d["n_treatments"] == 0 and d["billed"] == 0:
        treat = 0.0
    else:
        treat = 6.0
        if d["distinct_providers"] >= 2:
            treat += 3
        if d["distinct_providers"] >= 3:
            treat += 1
        if d["any_ongoing"]:
            treat += 3
        b = d["billed"]
        treat += 0 if b < 1000 else 1 if b < 5000 else 2 if b < 15000 else 3
    treat = _clamp(treat, 0, 16)
    reasoning.append(f"Treatment depth: {treat:.0f}/16")

    # 4. Economic damages (14) — specials, capped by available coverage when known
    # (recovery is bounded by coverage, so an undercovered case scores lower here).
    specials = d["reconciled_medical"] + d["future_medical"] + d["lost_wages"] + d["lost_earning_capacity"]
    if d["coverage_known"] and d["available_coverage"] > 0:
        specials = min(specials, d["available_coverage"])
    if specials <= 0:
        econ = 3.0 if d["total_damages"] > 0 else 0.0
    else:
        econ = 3 if specials < 2500 else 6 if specials < 10000 else 9 if specials < 25000 \
            else 12 if specials < 75000 else 14
    econ = _clamp(econ, 0, 14)
    reasoning.append(f"Economic damages: {econ:.0f}/14 (${specials:,.0f} specials)")

    # 5. Coverage availability (14). Use the SAME recoverable-coverage figure the settlement
    # engine uses (derive.available_coverage), so scoring and settlement can never contradict
    # (e.g. a UIM that doesn't apply without at-fault liability isn't counted here either).
    cov_amt = d["available_coverage"]
    if not d["coverage_known"]:
        cov = 4.0
        reasoning.append("Coverage availability: 4/14 (unknown — verify)")
    elif cov_amt <= 0:
        cov = 2.0 if d["medpay"] > 0 else 0.0
        reasoning.append(f"Coverage availability: {cov:.0f}/14 (no recoverable coverage identified)")
    else:
        cov = 6 if cov_amt < 25000 else 9 if cov_amt < 100000 else 12 if cov_amt < 300000 else 14
        reasoning.append(f"Coverage availability: {cov:.0f}/14 (${cov_amt:,.0f} available)")

    # 6. Case-type base (8)
    base = _CASE_PRIOR.get(f.case_type, 3)
    if f.case_type in _HIGH_TIER and base > 6:
        text = ((f.fault_narrative or "") + " " + (f.ai_summary or "")).lower()
        corro = any(k in text for k in _CORROBORATE_KW.get(f.case_type, ()))
        if f.case_type in ("Truck Accident", "Rideshare Accident"):
            corro = corro or any(p.get("party_role") == "at_fault" and p.get("carrier_name") for p in f.policies)
        if not corro:
            base = 6
    reasoning.append(f"Case-type prior: {base}/8 ({f.case_type})")

    # 7. Recency / SOL (6)
    if f.incident_date is None:
        rec = 4.0
    else:
        age = (today - f.incident_date).days
        if age < 0:
            rec = 4.0
        else:
            rec = 6 if age <= 180 else 5 if age <= 365 else 3 if age <= 729 \
                else 1 if age <= 1094 else 0
    reasoning.append(f"Recency/SOL: {rec:.0f}/6")

    raw = liab + sev + treat + econ + cov + base + rec
    final = raw
    represented = has_attorney_flag(f)
    if represented:
        final = min(raw, 15)
        reasoning.append(f"Representation conflict: capped {raw:.0f} -> {final:.0f}")

    final = int(round(_clamp(final, 0, 100)))
    raw_i = int(round(_clamp(raw, 0, 100)))

    # Temperature bands + defensive (push-down-only) guards.
    if final >= 75:
        temp = "Hot"
    elif final >= 55:
        temp = "Warm"
    elif final >= 35:
        temp = "Low"
    else:
        temp = "Poor Fit"
    order = ["Poor Fit", "Low", "Warm", "Hot"]

    def cap_temp(cur: str, ceiling: str) -> str:
        return ceiling if order.index(cur) > order.index(ceiling) else cur

    if represented:
        temp = "Poor Fit"
    if d["n_injuries"] == 0 or (d["n_treatments"] == 0 and d["billed"] == 0):
        temp = cap_temp(temp, "Low")
    if d["coverage_known"] and d["available_coverage"] <= 0 and d["max_sev"] >= 3:
        temp = cap_temp(temp, "Warm")

    return {"score": final, "score_raw": raw_i, "temperature": temp,
            "reasoning": reasoning, "model": "rules-v2"}
