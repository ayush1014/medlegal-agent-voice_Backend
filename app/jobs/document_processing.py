"""Server-side document processor — drains `document.received` outbox events.

For each received file: classify by content (gpt-4o vision / text), summarize, match it
to the requirement it satisfies (or route to human review — never a blind match), mine
structured data into the fact tables, and re-run the estimate. Decoupled + retryable, so
a vision-API hiccup never loses a client's document. Mirrors jobs/post_call.py.
"""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import NEON_CONNECT_ARGS, _build_async_url, session_scope
from app.security.context import system_context
from app.services import document_ai, document_service, lead_intelligence

logger = logging.getLogger("medlegal.document_processing")

MAX_ATTEMPTS = 5
_BACKOFF_SECONDS = 30


def _as_dict(payload) -> dict:
    if isinstance(payload, dict):
        return payload
    try:
        return json.loads(payload) if payload else {}
    except (TypeError, ValueError):
        return {}


def _num(x) -> float:
    try:
        return float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


async def _exists(db, sql: str, params: dict) -> bool:
    return (await db.execute(text(sql), params)).first() is not None


async def _match_requirement(db, lead_id, doc_id, category: str, confidence: float) -> str:
    """Link the doc to the outstanding requirement it satisfies + mark it Received.
    Returns the match_status: matched | needs_review | unmatched."""
    keywords = document_ai.CATEGORY_REQUIREMENT_KEYWORDS.get(category)
    if not keywords or confidence < document_ai.AUTO_MATCH_CONFIDENCE:
        return "needs_review"
    reqs = (await db.execute(
        text("SELECT id, document_type FROM document_requests WHERE lead_id=:l "
             "AND status NOT IN ('Received','Waived')"), {"l": lead_id})).all()
    for kw in keywords:
        for r in reqs:
            if kw in (r.document_type or "").lower():
                await db.execute(text("UPDATE documents SET document_request_id=:rid WHERE id=:d"),
                                 {"rid": r.id, "d": doc_id})
                await db.execute(text("UPDATE document_requests SET status='Received' WHERE id=:rid"),
                                 {"rid": r.id})
                return "matched"
    return "unmatched"  # confidently classified, but no matching outstanding ask


async def _apply_extracted(db, org, lead_id, category: str, extracted: dict) -> None:
    """Fold a classified doc's structured data into the fact tables (deduped) so the
    re-estimate is evidence-backed. Phase 1: medical bills + insurance declarations."""
    if category == "medical_bill":
        amt = _num(extracted.get("billed_amount"))
        if amt > 0 and not await _exists(
                db, "SELECT 1 FROM damages WHERE lead_id=:l AND category='medical' AND amount=:a",
                {"l": lead_id, "a": amt}):
            await db.execute(
                text("INSERT INTO damages (organization_id, lead_id, category, description, amount, "
                     "is_estimated) VALUES (:o,:l,'medical',:d,:a,false)"),
                {"o": org, "l": lead_id,
                 "d": f"Medical bill — {extracted.get('provider') or 'provider'} (from document)", "a": amt})
    elif category == "insurance_dec":
        carrier = (extracted.get("carrier") or "").strip() or None
        kind = extracted.get("policy_kind") if extracted.get("policy_kind") in (
            "Liability", "UM", "UIM", "MedPay", "Health", "Other") else None
        limit = _num(extracted.get("coverage_limit")) or None
        party = extracted.get("party_role") if extracted.get("party_role") in (
            "claimant", "at_fault", "other") else "other"
        if (carrier or limit) and not await _exists(
                db, "SELECT 1 FROM insurance_policies WHERE lead_id=:l AND coalesce(lower(carrier_name),'')"
                    "=coalesce(lower(:c),'') AND coalesce(policy_kind,'')=coalesce(:k,'')",
                {"l": lead_id, "c": carrier, "k": kind}):
            await db.execute(
                text("INSERT INTO insurance_policies (organization_id, lead_id, party_role, carrier_name, "
                     "policy_kind, coverage_limit, claim_number) VALUES (:o,:l,:pr,:c,:k,:lim,:cn)"),
                {"o": org, "l": lead_id, "pr": party, "c": carrier, "k": kind, "lim": limit,
                 "cn": (extracted.get("claim_number") or None)})
    elif category == "police_report":
        await db.execute(
            text("UPDATE incidents SET police_report_available=true WHERE lead_id=:l "
                 "AND id = (SELECT id FROM incidents WHERE lead_id=:l ORDER BY created_at LIMIT 1)"),
            {"l": lead_id})


async def _process_one(org, lead_id, doc_id, pre_matched: bool) -> None:
    # --- read metadata (short) ---
    async with session_scope(system_context(org)) as db:
        doc = (await db.execute(
            text("SELECT file_name, mime_type, storage_url FROM documents WHERE id=:d"),
            {"d": doc_id})).first()
    if doc is None or not doc.storage_url:
        raise RuntimeError(f"document {doc_id} not found / no storage_url")

    # --- network: download + classify (no tx held) ---
    import asyncio
    content = await asyncio.to_thread(document_service.load_object, doc.storage_url)
    result = await document_ai.classify(doc.file_name or "upload", content, doc.mime_type)

    # --- one tx: persist classification + match + facts + re-estimate ---
    async with session_scope(system_context(org)) as db:
        if pre_matched:
            match_status = "matched"  # caller already tagged + linked the requirement
        else:
            match_status = await _match_requirement(
                db, lead_id, doc_id, result["category"], result["confidence"])
        await db.execute(
            text("UPDATE documents SET doc_category=:c, classification_confidence=:conf, "
                 "doc_summary=:s, extracted=CAST(:ex AS jsonb), match_status=:ms WHERE id=:d"),
            {"c": result["category"], "conf": result["confidence"], "s": result["summary"],
             "ex": json.dumps(result["extracted"]), "ms": match_status, "d": doc_id})
        await _apply_extracted(db, org, lead_id, result["category"], result["extracted"])
        await document_service._recompute_missing(db, lead_id)
        # Re-estimate now that evidence may have changed the facts.
        await lead_intelligence.run_for_lead(db, lead_id)
    logger.info("document %s classified=%s conf=%.2f match=%s", doc_id,
                result["category"], result["confidence"], match_status)


async def process_pending_documents(limit: int = 10) -> dict:
    """Drain pending `document.received` events. Returns {processed, failed}."""
    result = {"processed": 0, "failed": 0}
    engine = create_async_engine(_build_async_url(settings.database_url), poolclass=NullPool, connect_args=NEON_CONNECT_ARGS)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(
                text("SELECT id, organization_id, aggregate_id, payload, attempts FROM outbox_events "
                     "WHERE status='pending' AND event_type='document.received' AND available_at <= now() "
                     "ORDER BY available_at LIMIT :n"), {"n": limit})).all()
        for r in rows:
            try:
                p = _as_dict(r.payload)
                await _process_one(
                    r.organization_id, uuid.UUID(p["lead_id"]), r.aggregate_id,
                    bool(p.get("pre_matched")))
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE outbox_events SET status='published', published_at=now(), "
                             "attempts=attempts+1 WHERE id=:id"), {"id": r.id})
                result["processed"] += 1
            except Exception:  # noqa: BLE001 - isolate failure to this event
                give_up = (r.attempts + 1) >= MAX_ATTEMPTS
                logger.exception("document processing failed for event %s (attempt %d%s)",
                                 r.id, r.attempts + 1, ", giving up" if give_up else ", will retry")
                async with engine.begin() as conn:
                    await conn.execute(
                        text("UPDATE outbox_events SET status=:st, attempts=attempts+1, "
                             "available_at = now() + make_interval(secs => :b) WHERE id=:id"),
                        {"st": "failed" if give_up else "pending",
                         "b": _BACKOFF_SECONDS * (r.attempts + 1), "id": r.id})
                result["failed"] += 1
    finally:
        await engine.dispose()
    return result
