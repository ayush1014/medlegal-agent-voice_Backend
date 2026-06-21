"""Single source of truth for the KG relation/role vocabulary and the leads
placeholder sentinels. Imported by BOTH the writer (memory_service.build_case_graph)
and the reader (context_service) so the graph contract cannot drift."""

from __future__ import annotations

RELATIONS = ("injured_in", "suffered", "treated_by", "insured_by")
CLIENT_ROLE = "client"
PARTY_ROLES = ("at_fault", "witness", "passenger")
PARTY_FALLBACK_RELATION = "involved_in"  # writer emits this when a party role is None
CLAIMANT_ROLE = "claimant"

# A name/case_type equal to one of these is "unknown", not real data.
NAME_PLACEHOLDERS = frozenset({"caller", "client", ""})
CASE_TYPE_PLACEHOLDER = "Other Personal Injury"


def is_placeholder_name(s: str | None) -> bool:
    return s is None or s.strip().lower() in NAME_PLACEHOLDERS


def is_placeholder_case_type(s: str | None) -> bool:
    return s is None or s.strip() == CASE_TYPE_PLACEHOLDER
