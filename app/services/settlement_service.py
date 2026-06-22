"""Settlement estimation — hybrid (deterministic baseline + bounded LLM nudge).

Deterministic Stage A-C produces a defensible low/expected/high anchored in PI
economics: specials -> general damages (severity multiplier) -> weak-case haircut
-> comparative fault -> coverage cap -> spread by data completeness. Stage D lets
an LLM nudge EXPECTED within [0.70, 1.20], then code re-clamps. Hard-blocked leads
(represented / unqualified) get a suppressed zero estimate.
"""

from __future__ import annotations

from datetime import date

from app.config import settings
from app.services import jurisdiction
from app.services.lead_facts import Facts, derive

# Pain-multiplier band midpoints by worst injury rank.
_PM_MID = {0: 0.0, 1: 1.5, 2: 2.625, 3: 4.0, 4: 6.25}
_BANDS = ["Low", "Medium", "High"]

# Severity-anchored general-damages (pain & suffering) floor, used when specials aren't
# documented yet so a treated, genuinely-injured client isn't valued at ~$0 on the first
# call. Conservative priors keyed by worst injury rank; inert once real specials push
# general above them. Tunable — calibration against firm outcomes will refine these.
_GENERAL_FLOOR = {0: 0.0, 1: 0.0, 2: 7_500.0, 3: 30_000.0, 4: 75_000.0}
_SURGERY_FLOOR_BUMP = 15_000.0
_WD_FLOOR = 75_000.0  # wrongful death: top-tier provisional floor (always Low confidence)


def _round_money(v: float) -> float:
    if v <= 0:
        return 0.0
    step = 500 if v < 50000 else 1000
    return float(round(v / step) * step)


def _completeness(f: Facts, d: dict) -> float:
    c = 0.0
    if any(i.get("severity") for i in f.injuries):
        c += 0.20
    if d["reconciled_medical"] > 0 or d["billed"] > 0:
        c += 0.20
    if d["coverage_known"]:
        c += 0.15
    if f.incident_date is not None:
        c += 0.10
    if d["liability_signal"]:
        c += 0.10
    if f.comparative_negligence_pct is not None:
        c += 0.10
    if any(x.get("category") in ("lost_wages", "lost_earning_capacity") for x in f.damages):
        c += 0.10
    if len(f.damages) >= 2:
        c += 0.05
    return min(c, 1.0)


def _first_treatment_gap_days(f: Facts) -> int | None:
    if f.incident_date is None:
        return None
    starts = [t.get("start_date") for t in f.treatments if t.get("start_date")]
    if not starts:
        return None
    try:
        return (min(starts) - f.incident_date).days
    except TypeError:
        return None


def estimate(
    f: Facts, *, today: date | None = None, hard_block: bool = False,
    qual_reason: str = "", use_llm: bool = False,
) -> dict:
    today = today or date.today()
    d = derive(f)

    if hard_block:
        return {
            "low": 0.0, "expected": 0.0, "high": 0.0, "confidence": "Low",
            "pain_multiplier": None, "model": "rules-v2",
            "reasoning": f"No settlement estimate — lead hard-blocked at qualification ({qual_reason}).",
            "inputs_snapshot": {"hard_block": True},
        }

    eligible_specials = d["eligible_specials"]
    property_d = d["property"]
    other_d = d["other_economic"]

    # A2-A3 general damages.
    n_inj = d["n_injuries"]
    pm = _PM_MID[d["max_sev"]] if not (d["max_sev"] == 0 and n_inj > 0) else 1.5
    if n_inj == 0:
        pm = 0.0
    general = eligible_specials * pm

    # A4 weak-case haircut.
    no_injuries = n_inj == 0
    no_treatment = d["n_treatments"] == 0 and d["reconciled_medical"] == 0 and d["future_medical"] == 0
    conf_floor_low = False
    if no_injuries and no_treatment:
        general = 0.0
        conf_floor_low = True
    elif no_injuries or no_treatment:
        general *= 0.25

    # A4b severity floor — a treated, genuinely-injured client (or a wrongful death) must
    # not value at ~$0 just because specials aren't documented yet. Conservative prior,
    # inert once real specials push general above it. Requires treatment (no floor for an
    # untreated injury); wrongful death gets the top floor regardless of injury rows.
    severity_floor = _GENERAL_FLOOR.get(d["max_sev"], 0.0)
    if d["any_surgery"]:
        severity_floor += _SURGERY_FLOOR_BUMP
    if f.case_type == "Wrongful Death":
        severity_floor = max(severity_floor, _WD_FLOOR)
    floor_eligible = (n_inj > 0 and not no_treatment) or f.case_type == "Wrongful Death"
    floor_applied = floor_eligible and severity_floor > general
    if floor_applied:
        general = severity_floor

    # A5 comparative fault — state-aware regime (single source of truth). The regime's
    # recovery factor is (100-pct)/100 when not barred, and 0 when the plaintiff's fault
    # bars recovery (contributory at any fault; modified at/over the threshold; pure never).
    comp = f.comparative_negligence_pct
    pct = max(0, min(100, comp if comp is not None else 0))
    bar = jurisdiction.comparative_bar(f.incident_state, comp)
    unknown_haircut = 0.90 if comp is None else 1.0
    gross = eligible_specials + general + property_d + other_d
    net = gross * bar["factor"] * unknown_haircut
    gap = _first_treatment_gap_days(f)
    if no_treatment and not floor_applied:
        net *= 0.25
    elif gap is not None and gap > 90:
        net *= 0.85

    # A6 coverage cap.
    available = d["available_coverage"]
    coverage_known = d["coverage_known"]
    if coverage_known:
        if available <= 0:
            cap_val = property_d + d["medpay"]
            expected_base = min(net, cap_val)
            if expected_base <= 0 and eligible_specials > 0:
                expected_base = min(eligible_specials, d["medpay"])
            coverage_binding = True
        else:
            coverage_binding = net > available
            expected_base = min(net, available)
    else:
        # Unknown coverage: soft ceiling at 3x specials so we never project a fantasy —
        # but never below a severity-floored general, or the floor would be cancelled out.
        soft_ceiling = max(eligible_specials * 3.0, general) if floor_applied else eligible_specials * 3.0
        expected_base = min(net, soft_ceiling)
        coverage_binding = False

    expected = expected_base
    if coverage_known and coverage_binding and available > 0:
        expected = min(expected_base, available * 0.95)

    # B spread by completeness.
    completeness = _completeness(f, d)
    lo_f, hi_f = (0.75, 1.25) if completeness >= 0.80 else (0.65, 1.45) if completeness >= 0.55 else (0.50, 1.70)
    low_raw = expected_base * lo_f
    high_raw = expected_base * hi_f
    if coverage_known and coverage_binding:
        high = min(high_raw, available)
        low = max(low_raw, expected_base * 0.85)
    elif coverage_known and not coverage_binding:
        high = min(high_raw, available)
        low = low_raw
    else:
        high_ceiling = max(eligible_specials * 3.0, general * 1.7) if floor_applied else eligible_specials * 3.0
        high = min(high_raw, high_ceiling)
        low = low_raw

    low, expected, high = _round_money(low), _round_money(expected), _round_money(high)
    low, expected, high = sorted([low, expected, high])  # rounding/ceilings can invert

    # C confidence.
    weak_case = no_injuries or no_treatment
    if completeness >= 0.80 and coverage_known and not weak_case:
        band = "High"
    elif completeness >= 0.55 and not (no_injuries and no_treatment):
        band = "Medium"
    else:
        band = "Low"
    def cap_band(cur, ceil):
        return ceil if _BANDS.index(cur) > _BANDS.index(ceil) else cur
    if not coverage_known:
        band = cap_band(band, "Medium")
    if weak_case:
        band = cap_band(band, "Medium")
    if conf_floor_low or (no_injuries and no_treatment):
        band = "Low"
    if comp is None:
        band = cap_band(band, "Medium")
    if floor_applied:
        band = cap_band(band, "Medium")  # severity-floored value is a prior, not evidence
    if floor_applied and f.case_type == "Wrongful Death":
        band = "Low"
    if bar["barred"]:
        band = "Low"  # likely barred under the state's rule, but the fault % is unverified

    pm_eff = max(0.0, min(8.0, pm))
    reasoning = (
        f"Specials ${eligible_specials:,.0f} x pain multiplier {pm_eff:.2f} = general "
        f"${general:,.0f}; comparative fault {pct}%"
        + ("" if comp is not None else " (unverified, -10%)")
        + (f"; {bar['regime'].replace('_', ' ')} rule BARS recovery at this fault — verify"
           if bar["barred"] else "")
        + ("; general anchored on a severity baseline (specials not yet documented — provisional)"
           if floor_applied else "")
        + (f"; capped by ${available:,.0f} available coverage" if coverage_known and coverage_binding
           else "; coverage unknown (soft ceiling)" if not coverage_known else "")
        + f". Expected ${expected:,.0f} (confidence {band})."
    )
    snapshot = {
        "eligible_specials": eligible_specials, "pain_multiplier": pm_eff, "general": general,
        "comparative_pct": pct, "unknown_fault_haircut": unknown_haircut, "net_after_fault": net,
        "available_coverage": available, "coverage_known": coverage_known,
        "coverage_binding": coverage_binding, "completeness": completeness,
        "floor_applied": floor_applied, "severity_floor": severity_floor,
    }

    result = {"low": low, "expected": expected, "high": high, "confidence": band,
              "pain_multiplier": round(pm_eff, 2), "model": "rules-v2",
              "reasoning": reasoning, "inputs_snapshot": snapshot}

    if use_llm and expected > 0:
        try:
            result = _llm_adjust(f, d, result)
        except Exception:  # noqa: BLE001 - deterministic baseline stands on any LLM failure
            pass
    return result


def _llm_adjust(f: Facts, d: dict, base: dict) -> dict:
    """Stage D: LLM is an untrusted suggester; it may nudge EXPECTED within
    [0.70, 1.20] for liability/case nuance. Code re-clamps to [low, high]."""
    import json

    from openai import OpenAI

    client = OpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)
    prompt = (
        "You adjust a personal-injury settlement EXPECTED value for liability/case nuance only. "
        "Return JSON {\"liability_adjust_factor\": x, \"rationale\": \"...\"} with x in [0.70, 1.20]. "
        "Use <1.0 for weak/contested liability, >1.0 for clear liability + sympathetic facts. "
        "Do NOT change the dollar figure directly.\n\n"
        f"case_type={f.case_type}; fault_narrative={f.fault_narrative}; summary={f.ai_summary}; "
        f"baseline_expected={base['expected']}; confidence={base['confidence']}."
    )
    resp = client.chat.completions.create(
        model=settings.deepseek_realtime_model, temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    client.close()
    data = json.loads(resp.choices[0].message.content or "{}")
    factor = float(data.get("liability_adjust_factor", 1.0))
    factor = max(0.70, min(1.20, factor))
    adjusted = max(base["low"], min(base["high"], _round_money(base["expected"] * factor)))
    base["expected"] = adjusted
    base["model"] = "rules-v2+llm"
    base["reasoning"] += f" LLM liability factor {factor:.2f}: {data.get('rationale', '')[:160]}"
    return base
