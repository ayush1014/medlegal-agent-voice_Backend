"""Settlement calibration metrics (scoring-plan #5). Pure, DB-free."""
from __future__ import annotations

from app.services.calibration_service import VALID_OUTCOMES, compute_calibration


def test_empty_is_safe():
    r = compute_calibration([])
    assert r["overall"] == {"n": 0, "mae": None, "bias": None, "mape": None, "within_band_rate": None}
    assert r["by_case_type"] == {} and r["by_confidence"] == {}


def test_metrics_math():
    recs = [
        {"predicted": 10000, "actual": 8000, "case_type": "Auto Accident", "confidence": "High",
         "low": 7000, "high": 12000},
        {"predicted": 5000, "actual": 6000, "case_type": "Auto Accident", "confidence": "Medium",
         "low": 4000, "high": 5500},
    ]
    o = compute_calibration(recs)["overall"]
    assert o["n"] == 2
    assert o["mae"] == 1500.0          # |+2000|, |-1000| → 1500
    assert o["bias"] == 500.0          # +2000, -1000 → +500 (slight over-prediction)
    assert abs(o["mape"] - 0.2083) < 0.001   # 0.25, 0.1667 → ~0.2083
    assert o["within_band_rate"] == 0.5      # 8000∈[7000,12000] yes; 6000∈[4000,5500] no


def test_grouping_by_case_type_and_confidence():
    recs = [
        {"predicted": 100, "actual": 100, "case_type": "Dog Bite", "confidence": "High"},
        {"predicted": 200, "actual": 100, "case_type": "Slip and Fall", "confidence": "Low"},
    ]
    r = compute_calibration(recs)
    assert set(r["by_case_type"]) == {"Dog Bite", "Slip and Fall"}
    assert r["by_case_type"]["Dog Bite"]["bias"] == 0.0
    assert set(r["by_confidence"]) == {"High", "Low"}


def test_actual_zero_skipped_in_mape():
    r = compute_calibration([{"predicted": 500, "actual": 0,
                              "case_type": "Other Personal Injury", "confidence": "Low"}])
    assert r["overall"]["n"] == 1
    assert r["overall"]["mape"] is None      # actual == 0 excluded from MAPE
    assert r["overall"]["mae"] == 500.0


def test_within_band_only_counts_banded_rows():
    r = compute_calibration([{"predicted": 100, "actual": 90, "case_type": "X", "confidence": "Low"}])
    assert r["overall"]["within_band_rate"] is None   # no low/high provided


def test_valid_outcomes():
    assert set(VALID_OUTCOMES) == {"settled", "dropped", "lost", "referred_out"}
