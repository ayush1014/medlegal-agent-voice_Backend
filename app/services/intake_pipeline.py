"""Post-call pipeline (PRD-2 handoff).

Runs when a call ends: extract structured facts (E), build case memory (F), emit
`intake.completed` for PRD-3 + capture usage/cost, and send the welcome SMS whose
OTP tap reuses the PRD-1 claim path (H). Network calls (LLM, embeddings, SMS)
happen outside the DB transaction; all persistence is one atomic tx.
"""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import text

from app.agent.context import IntakeContext
from app.agent.embeddings import embed_texts
from app.agent.extraction import (
    extract_from_transcript,
    generate_case_brief,
    merge_summaries,
)
from app.database import session_scope
from app.security.context import system_context
from app.services import (
    cost_service,
    document_service,
    extraction_service,
    lead_intelligence,  # noqa: F401 - registers the intake.completed handler
    memory_service,
    outbox_publisher,
    outbox_service,
    sms_service,
)

logger = logging.getLogger("medlegal.intake_pipeline")


def _agent_chars(transcript: str) -> int:
    return sum(len(line) for line in transcript.splitlines() if line.startswith("Agent:"))


async def _case_facts_text(db, lead_id: uuid.UUID) -> str:
    """A compact, readable snapshot of EVERYTHING known about the case — all calls'
    accumulated structured facts + the cumulative summary + funnel score/value — for
    the attorney brief. Regenerating from this each call makes the brief cumulative."""
    lead = (await db.execute(text(
        "SELECT full_name, case_type, ai_summary, occupation, employer, employment_status, "
        "annual_income, lead_score, qualification_status, settlement_expected, retainer_status "
        "FROM leads WHERE id=:l"), {"l": lead_id})).mappings().first()
    if lead is None:
        return ""
    incidents = (await db.execute(text(
        "SELECT incident_date, location_text, description, police_report_available, "
        "fault_narrative, comparative_negligence_pct FROM incidents WHERE lead_id=:l "
        "ORDER BY created_at"), {"l": lead_id})).mappings().all()
    injuries = (await db.execute(text(
        "SELECT body_part, severity, description, is_permanent, requires_surgery FROM injuries "
        "WHERE lead_id=:l ORDER BY created_at"), {"l": lead_id})).mappings().all()
    treatments = (await db.execute(text(
        "SELECT provider_name, provider_type, treatment_type, is_ongoing, billed_amount "
        "FROM medical_treatments WHERE lead_id=:l ORDER BY created_at"), {"l": lead_id})).mappings().all()
    damages = (await db.execute(text(
        "SELECT category, description, amount, is_estimated FROM damages WHERE lead_id=:l "
        "ORDER BY created_at"), {"l": lead_id})).mappings().all()
    parties = (await db.execute(text(
        "SELECT role, full_name, notes FROM parties WHERE lead_id=:l ORDER BY created_at"),
        {"l": lead_id})).mappings().all()

    p = [f"CLIENT: {lead['full_name']} | case type: {lead['case_type']}"]
    if lead["occupation"] or lead["annual_income"]:
        p.append(f"EMPLOYMENT: {lead['occupation'] or '—'} at {lead['employer'] or '—'} "
                 f"({lead['employment_status'] or '—'}); income {lead['annual_income'] or '—'}")
    p.append(f"FUNNEL: score {lead['lead_score']}; qualification {lead['qualification_status']}; "
             f"expected settlement {lead['settlement_expected'] or '—'}; retainer {lead['retainer_status']}")
    if lead["ai_summary"]:
        p.append(f"SUMMARY SO FAR: {lead['ai_summary']}")
    for i in incidents:
        cn = i["comparative_negligence_pct"]
        p.append(f"INCIDENT: date {i['incident_date'] or '—'} at {i['location_text'] or '—'} — "
                 f"{i['description'] or '—'}. Police report: {i['police_report_available']}. "
                 f"Fault: {i['fault_narrative'] or '—'}. Comparative negligence: "
                 f"{cn if cn is not None else '—'}%")
    for j in injuries:
        flags = ", ".join(f for f, on in (("permanent", j["is_permanent"]),
                                          ("surgery", j["requires_surgery"])) if on)
        p.append(f"INJURY: {j['body_part'] or '—'} ({j['severity'] or '—'}) "
                 f"{j['description'] or ''} {'[' + flags + ']' if flags else ''}".strip())
    for t in treatments:
        p.append(f"TREATMENT: {t['provider_name'] or '—'} ({t['provider_type'] or '—'}) "
                 f"{t['treatment_type'] or ''}; ongoing={t['is_ongoing']}; billed={t['billed_amount'] or '—'}")
    for d in damages:
        p.append(f"DAMAGE: {d['category']} — {d['description'] or ''} amount {d['amount'] or '—'} "
                 f"{'(estimated)' if d['is_estimated'] else ''}".strip())
    for pa in parties:
        p.append(f"PARTY: {pa['role']} — {pa['full_name'] or 'unidentified'} "
                 f"{pa['notes'] or ''}".strip())
    return "\n".join(p)


async def _refresh_case_brief(organization_id: uuid.UUID, lead_id: uuid.UUID) -> None:
    """Regenerate the attorney brief from the full accumulated record and persist it.

    Runs AFTER scoring so the brief can reference the funnel score/value. The brief is
    internal-only (lead detail); failures are non-fatal.
    """
    async with session_scope(system_context(organization_id)) as db:
        facts = await _case_facts_text(db, lead_id)
    brief = await generate_case_brief(facts)
    if not brief:
        return
    async with session_scope(system_context(organization_id)) as db:
        await db.execute(
            text("UPDATE leads SET case_brief = CAST(:b AS jsonb) WHERE id=:l"),
            {"b": json.dumps(brief), "l": lead_id},
        )


async def run_post_call_pipeline(
    *,
    organization_id: uuid.UUID,
    lead_id: uuid.UUID,
    transcript_text: str,
    transcript_id: uuid.UUID | None = None,
    voice_call_id: uuid.UUID | None = None,
    caller_phone: str | None = None,
    duration_seconds: int | None = None,
) -> dict:
    # --- Network (no DB tx held) ---
    extraction = await extract_from_transcript(transcript_text)
    chunks = memory_service.chunk_transcript(transcript_text)
    embeddings = await embed_texts(chunks)

    # Cumulative case summary: a returning caller's new summary must MERGE with the
    # prior one, not overwrite it. Build this call's summary (with a human-readable
    # representation note), read any existing summary, and fold them together — off the
    # tx. The authoritative representation flag is persisted separately as a column
    # (persist_extraction), so has_attorney is left intact here for that write.
    new_summary = (extraction.lead.summary or "").strip()
    if extraction.lead.has_attorney is not None:
        new_summary = (f"{new_summary} (Already represented: "
                       f"{'yes' if extraction.lead.has_attorney else 'no'}.)").strip()
    async with session_scope(system_context(organization_id)) as db:
        prior_summary = (await db.execute(
            text("SELECT ai_summary FROM leads WHERE id=:l"), {"l": lead_id})).scalar_one_or_none()
    extraction.lead.summary = await merge_summaries((prior_summary or "").strip(), new_summary)

    ctx = IntakeContext(
        organization_id=organization_id, caller_phone=caller_phone or "", lead_id=lead_id
    )
    cost = cost_service.estimate_call_cost(duration_seconds, _agent_chars(transcript_text))

    # --- One atomic transaction: extraction + memory + usage + outbox ---
    async with session_scope(system_context(organization_id)) as db:
        counts = await extraction_service.persist_extraction(db, ctx, extraction)
        n_chunks = await memory_service._store_chunks(
            db, ctx, transcript_id or uuid.uuid4(), chunks, embeddings
        )
        graph = await memory_service.build_case_graph(db, ctx, extraction)
        await cost_service.record_usage(db, ctx, cost)
        await outbox_service.emit_event(
            db, organization_id,
            aggregate_type="lead",
            aggregate_id=lead_id,
            event_type="intake.completed",
            payload={
                "lead_id": str(lead_id),
                "voice_call_id": str(voice_call_id) if voice_call_id else None,
                "case_type": extraction.lead.case_type,
                "extraction": counts,
                "chunks": n_chunks,
                "cost": cost,
            },
        )

    # --- Funnel trigger: dispatch intake.completed -> scoring/qualification/settlement ---
    try:
        await outbox_publisher.dispatch_pending_for_org(organization_id)
    except Exception:  # noqa: BLE001 - a sweep can retry; never break teardown
        pass

    # --- Attorney case brief: regenerate from the FULL accumulated record (structured
    # facts + cumulative summary + funnel score/value). Cumulative for returning callers,
    # internal-only (lead detail). Runs after scoring; non-fatal. ---
    try:
        await _refresh_case_brief(organization_id, lead_id)
    except Exception:  # noqa: BLE001 - never break teardown; brief can be regenerated
        logger.exception("case brief generation failed for lead %s", lead_id)

    # --- Welcome SMS → reuses the PRD-1 claim path on tap (network) ---
    if caller_phone:
        await sms_service.send_welcome_sms(
            organization_id, lead_id, caller_phone, email=extraction.lead.email
        )

    # --- Auto document request (email when we have one) for pursuable leads ---
    # Scoring set the pipeline above; skip clearly-rejected leads. request_documents
    # picks the channel (email if the lead has an address) and sends the checklist.
    try:
        async with session_scope(system_context(organization_id)) as db:
            pipeline = (await db.execute(
                text("SELECT pipeline_status FROM leads WHERE id=:l"), {"l": lead_id})).scalar_one_or_none()
        if pipeline and pipeline != "Rejected":
            await document_service.request_documents(organization_id, lead_id)
    except Exception:  # noqa: BLE001 - never break teardown; can be re-requested manually
        logger.exception("auto doc-request failed for lead %s", lead_id)

    return {"extraction": counts, "chunks": n_chunks, "graph": graph, "cost": cost}
