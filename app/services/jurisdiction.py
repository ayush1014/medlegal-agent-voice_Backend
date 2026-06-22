"""US jurisdiction reference for PI triage — statute-of-limitations + comparative-fault
regime by state.

IMPORTANT: these are SIGNALS for triage, NOT legal determinations. SOL has many
exceptions (the discovery rule, minors/tolling, short government-claim notice periods,
case-type specials), so callers must treat the output as an aging/risk signal and NEVER
auto-reject on it. Comparative-fault `pct` at intake is usually a rough, unverified
estimate, so a "barred" result routes a lead to human review — it never silently rejects.

FIRMS SHOULD VERIFY this table with counsel; values are a well-sourced starting point and
laws change (e.g. FL moved to modified-51% and shortened its PI SOL to 2yr in 2023;
LA extended its PI SOL to 2yr in 2024).
"""
from __future__ import annotations

# Comparative-fault regimes.
PURE = "pure"                 # recover at any fault %, reduced by your share (even 99%)
MODIFIED_50 = "modified_50"   # barred if you are 50% or more at fault
MODIFIED_51 = "modified_51"   # barred if you are 51% or more at fault
CONTRIBUTORY = "contributory"  # barred by ANY fault (1%+) — AL, MD, NC, VA, DC

# state -> (general personal-injury SOL years, comparative-fault regime). Verify w/ counsel.
_STATES: dict[str, tuple[int, str]] = {
    "AL": (2, CONTRIBUTORY), "AK": (2, PURE), "AZ": (2, PURE), "AR": (3, MODIFIED_50),
    "CA": (2, PURE), "CO": (2, MODIFIED_50), "CT": (2, MODIFIED_51), "DE": (2, MODIFIED_51),
    "FL": (2, MODIFIED_51), "GA": (2, MODIFIED_50), "HI": (2, MODIFIED_51), "ID": (2, MODIFIED_50),
    "IL": (2, MODIFIED_51), "IN": (2, MODIFIED_51), "IA": (2, MODIFIED_51), "KS": (2, MODIFIED_50),
    "KY": (1, PURE), "LA": (2, PURE), "ME": (6, MODIFIED_50), "MD": (3, CONTRIBUTORY),
    "MA": (3, MODIFIED_51), "MI": (3, MODIFIED_51), "MN": (6, MODIFIED_51), "MS": (3, PURE),
    "MO": (5, PURE), "MT": (3, MODIFIED_51), "NE": (4, MODIFIED_50), "NV": (2, MODIFIED_51),
    "NH": (3, MODIFIED_51), "NJ": (2, MODIFIED_51), "NM": (3, PURE), "NY": (3, PURE),
    "NC": (3, CONTRIBUTORY), "ND": (6, MODIFIED_50), "OH": (2, MODIFIED_51), "OK": (2, MODIFIED_51),
    "OR": (2, MODIFIED_51), "PA": (2, MODIFIED_51), "RI": (3, PURE), "SC": (3, MODIFIED_51),
    "SD": (3, MODIFIED_50), "TN": (1, MODIFIED_50), "TX": (2, MODIFIED_51), "UT": (4, MODIFIED_50),
    "VT": (3, MODIFIED_51), "VA": (2, CONTRIBUTORY), "WA": (3, PURE), "WV": (2, MODIFIED_50),
    "WI": (3, MODIFIED_51), "WY": (4, MODIFIED_51), "DC": (3, CONTRIBUTORY),
}

_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
    "washington dc": "DC", "washington d.c.": "DC", "d.c.": "DC", "dc": "DC",
}

_WRONGFUL_DEATH_SOL_YEARS = 2   # WD commonly ~2yr; verify per state


def normalize_state(value: str | None) -> str | None:
    """A free-text state ('New Jersey', 'NJ', 'nj') -> 2-letter code, or None."""
    if not value:
        return None
    v = value.strip()
    if len(v) == 2 and v.upper() in _STATES:
        return v.upper()
    return _NAME_TO_CODE.get(v.lower())


def known(state: str | None) -> bool:
    return (state or "").upper() in _STATES


def regime_of(state: str | None) -> str:
    """Comparative-fault regime; defaults to modified-51% (most common) when unknown."""
    entry = _STATES.get((state or "").upper())
    return entry[1] if entry else MODIFIED_51


def sol_years(state: str | None, case_type: str) -> int | None:
    """PI statute years for the state. Returns None for an unknown state (non-WD) so the
    caller can fall back to its generic aging bands instead of guessing a number."""
    if case_type == "Wrongful Death":
        return _WRONGFUL_DEATH_SOL_YEARS
    entry = _STATES.get((state or "").upper())
    return entry[0] if entry else None


def comparative_bar(state: str | None, pct: int | None) -> dict:
    """Whether the plaintiff's fault bars recovery under the state's regime, plus the
    recovery factor (multiplier on damages). Unknown pct -> treated as 0 (not barred)."""
    regime = regime_of(state)
    p = max(0, min(100, pct if pct is not None else 0))
    if regime == PURE:
        barred = False
    elif regime == CONTRIBUTORY:
        barred = p > 0
    elif regime == MODIFIED_50:
        barred = p >= 50
    else:  # MODIFIED_51
        barred = p >= 51
    return {"regime": regime, "barred": barred, "factor": 0.0 if barred else (100 - p) / 100}
