"""Controlled vocabularies (PRD §6).

Single source of truth for enum values on the backend. These MUST stay identical
to the frontend `src/lib/constants.ts`. We store them as ``varchar`` + ``CHECK``
constraints (not native Postgres enums) so the vocabulary can evolve via simple
migrations rather than fragile ``ALTER TYPE`` surgery.
"""

from __future__ import annotations

# --- Identity / tenancy ---------------------------------------------------
USER_ROLES = [
    "owner",
    "admin",
    "attorney",
    "paralegal",
    "intake_specialist",
    "client",
    "system",
]
SUBSCRIPTION_STATUSES = ["trial", "active", "past_due", "canceled"]
SUBJECT_TYPES = ["user", "client"]

# --- Core PI domain (used from Phase 2 onward) ----------------------------
CASE_TYPES = [
    "Auto Accident",
    "Truck Accident",
    "Motorcycle Accident",
    "Pedestrian Accident",
    "Rideshare Accident",
    "Slip and Fall",
    "Dog Bite",
    "Workplace Injury",
    "Premises Liability",
    "Wrongful Death",
    "Other Personal Injury",
]
QUALIFICATION_STATUSES = [
    "Qualified",
    "Possibly Qualified",
    "Needs Review",
    "Unqualified",
]
LEAD_TEMPERATURES = ["Hot", "Warm", "Low", "Poor Fit"]
PIPELINE_STATUSES = [
    "New Lead",
    "Intake Started",
    "Intake Complete",
    "Qualified",
    "Needs Review",
    "Docs Requested",
    "Docs Received",
    "Retainer Ready",
    "Retainer Sent",
    "Signed",
    "Rejected",
    "Closed",
]
RETAINER_STATUSES = [
    "Not Ready",
    "Ready",
    "Sent",
    "Viewed",
    "Signed",
    "Declined",
    "Expired",
]
SETTLEMENT_CONFIDENCES = ["Low", "Medium", "High"]
MESSAGE_CHANNELS = ["voice", "sms", "whatsapp", "email"]
MESSAGE_DIRECTIONS = ["inbound", "outbound"]
DOCUMENT_REQUEST_STATUSES = [
    "Pending",
    "Sent",
    "Partially Received",
    "Received",
    "Waived",
]
INJURY_SEVERITIES = ["Minor", "Moderate", "Severe", "Permanent"]
POLICY_KINDS = ["Liability", "UM", "UIM", "MedPay", "Health", "Other"]

# Where a lead originated.
LEAD_SOURCES = ["inbound_call", "web", "referral", "other"]

# Itemized damages (the inputs to the settlement math).
DAMAGE_CATEGORIES = [
    "medical",
    "future_medical",
    "lost_wages",
    "lost_earning_capacity",
    "property",
    "other",
]
DAMAGE_SOURCES = ["bill", "statement", "ai_estimate"]

# Parties and the side an insurance policy covers.
PARTY_ROLES = ["at_fault", "witness", "passenger", "other"]
POLICY_PARTY_ROLES = ["claimant", "at_fault", "other"]

# Intake transcript lifecycle + who is speaking in a segment.
TRANSCRIPT_STATUSES = ["in_progress", "complete", "failed"]
TRANSCRIPT_SPEAKERS = ["caller", "agent"]

# Author of an AI output row (a human user or the agent service).
CREATED_BY_TYPES = ["user", "system"]

# --- Communications (Phase 3) ---
MESSAGE_PURPOSES = ["intake", "follow_up", "doc_request", "retainer", "general"]
MESSAGE_STATUSES = ["queued", "sent", "delivered", "failed", "received"]

# --- Documents & retainer (Phase 3) ---
# Mirrors the frontend REQUESTABLE_DOCUMENTS list.
REQUESTABLE_DOCUMENTS = [
    "Police report",
    "Accident photos",
    "Injury photos",
    "Medical records",
    "Medical bills",
    "Insurance correspondence",
    "Driver's license",
    "Vehicle damage photos",
    "Witness information",
    "Employer wage loss documentation",
    "Prior settlement offers",
]
DOCUMENT_UPLOADED_BY = ["client", "user", "agent"]
DOCUMENT_SCAN_STATUSES = ["pending", "clean", "infected"]
SIGNATURE_EVENT_TYPES = ["sent", "viewed", "signed", "declined", "expired"]
SIGNATURE_ACTORS = ["client", "system"]

# --- Workflow & audit (Phase 3) ---
TASK_STATUSES = ["open", "done", "snoozed", "cancelled"]
AUDIT_ACTOR_TYPES = ["user", "client", "system"]

# --- AI memory (Phase 4) ---
# Embedding dimension — locked to OpenAI text-embedding-3-small (1536).
EMBEDDING_DIM = 1536
KNOWLEDGE_SOURCE_TYPES = [
    "transcript",
    "document",
    "note",
    "settlement",
    "medical",
    "message",
    "incident",
]
KG_NODE_TYPES = [
    "person",
    "injury",
    "provider",
    "insurer",
    "incident",
    "fact",
    "document",
]
AGENT_EVENT_TYPES = ["tool_call", "tool_result", "decision", "error"]

# --- Event backbone (Phase 4) ---
OUTBOX_STATUSES = ["pending", "published", "failed"]
WEBHOOK_STATUSES = ["received", "processed", "ignored"]


def sql_in(column: str, values: list[str]) -> str:
    """Render a CHECK-constraint membership clause, e.g. ``role IN ('owner', ...)``."""
    quoted = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return f"{column} IN ({quoted})"
