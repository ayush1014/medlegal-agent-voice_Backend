"""Severity-anchored general-damages floor (settlement step #2).

Pure unit tests of settlement_service.estimate — no DB, so they're fast and immune
to the shared-Neon test contention. They lock in: a treated, genuinely-injured client
isn't valued at ~$0 before bills are documented, the floor is inert once real specials
exceed it, coverage still caps, and weak/minor cases get no manufactured value.
"""
from __future__ import annotations

from app.services import settlement_service as s
from app.services.lead_facts import Facts


def _est(**kw):
    return s.estimate(Facts(**kw))


def test_floor_lifts_fresh_serious_injury_with_no_specials():
    r = _est(case_type="Auto Accident",
             injuries=[{"severity": "Severe", "requires_surgery": True}],
             treatments=[{"provider_name": "ER", "is_ongoing": True}],
             comparative_negligence_pct=0)
    assert r["inputs_snapshot"]["floor_applied"] is True
    assert r["expected"] >= 30000           # not ~$0 anymore
    assert r["confidence"] in ("Low", "Medium")  # a prior, never High
    assert r["low"] < r["expected"] < r["high"]  # real band, not degenerate


def test_floor_inert_once_specials_documented():
    r = _est(case_type="Auto Accident",
             injuries=[{"severity": "Severe", "requires_surgery": True}],
             treatments=[{"provider_name": "ER", "billed_amount": 40000}],
             damages=[{"category": "medical", "amount": 40000}],
             comparative_negligence_pct=0)
    assert r["inputs_snapshot"]["floor_applied"] is False  # specials x pm dominates
    assert r["expected"] > 45000


def test_coverage_cap_binds_over_floor():
    r = _est(case_type="Auto Accident",
             injuries=[{"severity": "Severe", "requires_surgery": True}],
             treatments=[{"provider_name": "ER"}],
             policies=[{"party_role": "at_fault", "policy_kind": "Liability", "coverage_limit": 15000}],
             comparative_negligence_pct=0)
    assert r["inputs_snapshot"]["floor_applied"] is True
    assert r["expected"] <= 15000           # known coverage still caps the floored value


def test_no_floor_for_minor_injury():
    r = _est(case_type="Auto Accident",
             injuries=[{"severity": "Minor"}],
             treatments=[{"provider_name": "ER"}])
    assert r["inputs_snapshot"]["floor_applied"] is False
    assert r["expected"] == 0


def test_no_floor_without_injury_or_treatment():
    r = _est(case_type="Other Personal Injury")
    assert r["inputs_snapshot"]["floor_applied"] is False
    assert r["expected"] == 0


def test_wrongful_death_gets_top_floor_at_low_confidence():
    r = _est(case_type="Wrongful Death")
    assert r["inputs_snapshot"]["floor_applied"] is True
    assert r["expected"] > 0
    assert r["confidence"] == "Low"


def test_policies_without_recoverable_limit_do_not_zero_the_estimate():
    # Policy rows exist (coverage_known) but none resolve to a recoverable limit —
    # only the client's own Liability + a UIM that needs at-fault liability to apply, so
    # available_coverage = 0. That's INCOMPLETE data, not confirmed $0 coverage: the
    # estimate must fall to the soft ceiling (severity floor survives), not crush to $0.
    r = _est(case_type="Slip and Fall",
             injuries=[{"severity": "Moderate"}],
             treatments=[{"provider_name": "ER"}],
             policies=[{"party_role": "claimant", "policy_kind": "Liability", "coverage_limit": 50000},
                       {"party_role": "claimant", "policy_kind": "UIM", "coverage_limit": 250000}])
    assert r["expected"] > 0
    assert "soft ceiling" in r["reasoning"]


def test_hard_block_suppresses_estimate_regardless_of_floor():
    r = s.estimate(Facts(case_type="Auto Accident",
                         injuries=[{"severity": "Severe", "requires_surgery": True}],
                         treatments=[{"provider_name": "ER"}]),
                   hard_block=True, qual_reason="represented")
    assert r["expected"] == 0 and r["inputs_snapshot"].get("hard_block") is True
