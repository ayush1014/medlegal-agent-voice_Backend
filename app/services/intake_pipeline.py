"""Post-call pipeline (PRD-2 handoff).

Runs when a call ends: extract structured facts (E), build case memory (F), emit
`intake.completed` for PRD-3 + capture usage/cost, and send the welcome SMS whose
OTP tap reuses the PRD-1 claim path (H). Network calls (LLM, embeddings, SMS)
happen outside the DB transaction; all persistence is one atomic tx.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text

from app.agent.context import IntakeContext
from app.agent.embeddings import embed_texts
from app.agent.extraction import extract_from_transcript, merge_summaries
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
    # prior one, not overwrite it. Build this call's summary (with the representation
    # note), read any existing summary, and fold them together — all off the tx.
    new_summary = (extraction.lead.summary or "").strip()
    if extraction.lead.has_attorney is not None:
        new_summary = (f"{new_summary} (Already represented: "
                       f"{'yes' if extraction.lead.has_attorney else 'no'}.)").strip()
    async with session_scope(system_context(organization_id)) as db:
        prior_summary = (await db.execute(
            text("SELECT ai_summary FROM leads WHERE id=:l"), {"l": lead_id})).scalar_one_or_none()
    extraction.lead.summary = await merge_summaries((prior_summary or "").strip(), new_summary)
    # The merged summary already carries the representation note → stop persist re-appending.
    extraction.lead.has_attorney = None

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
