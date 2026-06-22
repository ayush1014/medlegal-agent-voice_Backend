"""Settlement-model calibration (scoring-plan #5).

Compares the PREDICTED settlement against the ACTUAL on closed cases and reports where the
model is off — overall and broken down by case type and confidence band. Pure functions
(no DB), so the math is unit-tested directly; the endpoint just feeds it rows.

Metrics (all None-safe / empty-safe):
- mae:   mean absolute error in dollars (typical miss size)
- bias:  mean signed error (predicted - actual); >0 = over-predicting, <0 = under-predicting
- mape:  mean absolute % error (skips actual == 0)
- within_band_rate: share of actuals that fell inside the predicted low–high range (coverage)
"""
from __future__ import annotations

VALID_OUTCOMES = ("settled", "dropped", "lost", "referred_out")


def _metrics(rows: list[dict]) -> dict:
    rows = [r for r in rows if r.get("actual") is not None and r.get("predicted") is not None]
    n = len(rows)
    if n == 0:
        return {"n": 0, "mae": None, "bias": None, "mape": None, "within_band_rate": None}
    errs = [float(r["predicted"]) - float(r["actual"]) for r in rows]
    mae = sum(abs(e) for e in errs) / n
    bias = sum(errs) / n
    pcts = [abs(e) / float(r["actual"]) for r, e in zip(rows, errs) if r["actual"]]
    mape = sum(pcts) / len(pcts) if pcts else None
    banded = [r for r in rows if r.get("low") is not None and r.get("high") is not None]
    within = (sum(1 for r in banded if float(r["low"]) <= float(r["actual"]) <= float(r["high"]))
              / len(banded)) if banded else None
    return {
        "n": n,
        "mae": round(mae, 2),
        "bias": round(bias, 2),
        "mape": round(mape, 4) if mape is not None else None,
        "within_band_rate": round(within, 4) if within is not None else None,
    }


def compute_calibration(records: list[dict]) -> dict:
    """records: [{predicted, actual, case_type, confidence, low, high}] for closed cases.

    Returns {overall, by_case_type, by_confidence} of predicted-vs-actual metrics.
    """
    by_case_type = {}
    for ct in sorted({r.get("case_type") for r in records if r.get("case_type")}):
        by_case_type[ct] = _metrics([r for r in records if r.get("case_type") == ct])
    by_confidence = {}
    for cf in ("High", "Medium", "Low"):
        rs = [r for r in records if r.get("confidence") == cf]
        if rs:
            by_confidence[cf] = _metrics(rs)
    return {
        "overall": _metrics(records),
        "by_case_type": by_case_type,
        "by_confidence": by_confidence,
    }
