"""Jurisdiction-aware SOL + comparative fault (scoring step #3).

Pure unit tests — no DB, immune to the shared-Neon contention. They lock in the
state -> regime classification, the comparative-fault bar, state-aware SOL aging, and
that barred cases reflect in settlement (at Low confidence) and route to review in
qualification — never an auto-reject.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.services import jurisdiction as J
from app.services import qualification_service as Q
from app.services import settlement_service as S
from app.services.lead_facts import Facts, sol_signal

TODAY = date(2026, 6, 22)


def test_normalize_state():
    assert J.normalize_state("New Jersey") == "NJ"
    assert J.normalize_state("nj") == "NJ"
    assert J.normalize_state("CALIFORNIA") == "CA"
    assert J.normalize_state("Narnia") is None
    assert J.normalize_state(None) is None


def test_regime_classification_spot_checks():
    assert J.regime_of("AL") == J.CONTRIBUTORY
    assert J.regime_of("VA") == J.CONTRIBUTORY
    assert J.regime_of("CA") == J.PURE
    assert J.regime_of("NY") == J.PURE
    assert J.regime_of("NJ") == J.MODIFIED_51
    assert J.regime_of("CO") == J.MODIFIED_50
    assert J.regime_of("ZZ") == J.MODIFIED_51  # unknown -> most-common default


def test_comparative_bar_by_regime():
    assert J.comparative_bar("NY", 99)["barred"] is False          # pure: never barred
    assert J.comparative_bar("NY", 80)["factor"] == 0.20
    assert J.comparative_bar("VA", 1)["barred"] is True            # contributory: any fault
    assert J.comparative_bar("VA", 0)["barred"] is False
    assert J.comparative_bar("CO", 50)["barred"] is True           # modified-50 at 50
    assert J.comparative_bar("CO", 49)["barred"] is False
    assert J.comparative_bar("NJ", 51)["barred"] is True           # modified-51 at 51
    assert J.comparative_bar("NJ", 50)["barred"] is False


def test_sol_years_and_unknown_fallback():
    assert J.sol_years("TN", "Auto Accident") == 1
    assert J.sol_years("NY", "Auto Accident") == 3
    assert J.sol_years("XX", "Auto Accident") is None             # unknown -> caller falls back
    assert J.sol_years("CA", "Wrongful Death") == 2


def test_state_aware_sol_signal():
    eighteen_mo = TODAY - timedelta(days=547)
    assert sol_signal(eighteen_mo, TODAY, "Auto Accident", "TN") == "old"    # 1yr SOL
    assert sol_signal(eighteen_mo, TODAY, "Auto Accident", "NY") == "fresh"  # 3yr SOL
    # Unknown state keeps the original generic bands (no behaviour change).
    assert sol_signal(eighteen_mo, TODAY, "Auto Accident", None) == "fresh"
    assert sol_signal(TODAY - timedelta(days=900), TODAY, "Auto Accident", None) == "aging"


def _sev(state, pct):
    return Facts(case_type="Auto Accident", incident_state=state,
                 injuries=[{"severity": "Severe"}],
                 treatments=[{"provider_name": "ER", "billed_amount": 20000}],
                 damages=[{"category": "medical", "amount": 20000}],
                 comparative_negligence_pct=pct)


def test_settlement_pure_recovers_at_high_fault():
    r = S.estimate(_sev("NY", 80))
    assert r["expected"] > 0 and r["inputs_snapshot"]["net_after_fault"] > 0


def test_settlement_barred_regimes_zero_out():
    assert S.estimate(_sev("VA", 10))["expected"] == 0   # contributory
    assert S.estimate(_sev("NJ", 60))["expected"] == 0   # modified-51
    assert S.estimate(_sev("CO", 50))["expected"] == 0   # modified-50


def test_qualification_routes_barred_to_review():
    assert Q.qualify(_sev("VA", 10), today=TODAY)["status"] == "Needs Review"
    assert Q.qualify(_sev("NJ", 60), today=TODAY)["status"] == "Needs Review"
