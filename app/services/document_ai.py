"""Document AI — classify, summarize, and mine structured data from a client-submitted
file (email attachment or portal upload).

Classification is CONTENT-based: images are read by a multimodal model (gpt-4o vision)
and text-layer PDFs are read as text, so an arbitrarily-named photo of an insurance
letter is recognized for what it is — the filename is only a weak hint. Output is
advisory: low-confidence or unmatched files route to human review, never a blind match.
"""
from __future__ import annotations

import base64
import io
import json
import logging

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger("medlegal.document_ai")

# Categories the classifier may return (aligned with the requirement checklist).
DOC_CATEGORIES = [
    "medical_bill", "medical_record", "police_report", "insurance_dec",
    "accident_photo", "vehicle_damage", "injury_photo", "identification",
    "retainer", "other",
]

# category -> phrases matched against a lead's outstanding document_requests.document_type.
# Ordered most-specific first so 'vehicle_damage' prefers its own ask over 'accident photos'.
CATEGORY_REQUIREMENT_KEYWORDS = {
    "medical_bill": ["medical bills"],
    "medical_record": ["medical records"],
    "police_report": ["police report"],
    "insurance_dec": ["insurance correspondence", "insurance"],
    "vehicle_damage": ["vehicle damage photos", "vehicle damage"],
    "accident_photo": ["accident photos"],
    "injury_photo": ["injury photos"],
    "identification": ["identification"],
}

# Auto-match a requirement (mark Received) only at/above this confidence; else needs_review.
AUTO_MATCH_CONFIDENCE = 0.60

_PROMPT = (
    "You classify a file a client submitted to a personal-injury law firm. Decide what it "
    "IS from its CONTENT (the filename is only a weak hint). Output a single JSON object:\n"
    '{"category": one of ' + json.dumps(DOC_CATEGORIES) + ",\n"
    ' "confidence": a number 0.0-1.0 (confidence in the category),\n'
    ' "summary": "1-2 sentence plain description of what it is and the key contents",\n'
    ' "extracted": { structured fields present, else {} }}\n\n'
    "For a PHOTO, decide: an accident scene (accident_photo), damage to a vehicle "
    "(vehicle_damage), a visible bodily injury (injury_photo), or a PHOTO OF a document — "
    "then classify the document itself (medical_bill, insurance_dec, police_report, ...).\n"
    "Extract when clearly present (numbers as plain numbers — no $ or commas):\n"
    '  medical_bill -> {"provider": str, "billed_amount": number}\n'
    '  insurance_dec -> {"carrier": str, "policy_kind": "Liability|UM|UIM|MedPay|Health|Other",'
    ' "coverage_limit": number, "claim_number": str, "party_role": "claimant|at_fault"}\n'
    '  police_report -> {"report_number": str, "at_fault": str}\n'
    "Base everything ONLY on what is visible; never invent. Output only the JSON object."
)


def _clean(data: dict) -> dict:
    cat = data.get("category")
    if cat not in DOC_CATEGORIES:
        cat = "other"
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    extracted = data.get("extracted") if isinstance(data.get("extracted"), dict) else {}
    return {"category": cat, "confidence": max(0.0, min(1.0, conf)),
            "summary": str(data.get("summary") or "").strip()[:1000], "extracted": extracted}


async def _vision_classify(content: bytes, mime: str, file_name: str) -> dict:
    b64 = base64.b64encode(content).decode()
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        resp = await client.chat.completions.create(
            model=settings.vision_model, temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": f"Filename: {file_name}"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]},
            ],
        )
    finally:
        await client.close()
    return _clean(json.loads(resp.choices[0].message.content or "{}"))


async def _text_classify(text_content: str, file_name: str) -> dict:
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        resp = await client.chat.completions.create(
            model=settings.voice_llm_model, temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _PROMPT},
                {"role": "user",
                 "content": f"Filename: {file_name}\n\nDocument text:\n{text_content[:12000]}"},
            ],
        )
    finally:
        await client.close()
    return _clean(json.loads(resp.choices[0].message.content or "{}"))


def _pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:10]).strip()
    except Exception:  # noqa: BLE001 - unreadable/encrypted PDF -> no text layer
        return ""


async def classify(file_name: str, content: bytes, mime: str | None) -> dict:
    """Classify + summarize + mine a file -> {category, confidence, summary, extracted}.

    Never raises on content issues — returns a low-confidence 'other' result instead, which
    the worker routes to human review.
    """
    file_name = file_name or "upload"
    mime = (mime or "").lower()
    try:
        if mime.startswith("image/"):
            return await _vision_classify(content, mime, file_name)
        if mime == "application/pdf" or file_name.lower().endswith(".pdf"):
            text_content = _pdf_text(content)
            if len(text_content) >= 40:
                return await _text_classify(text_content, file_name)
            return {"category": "other", "confidence": 0.0,
                    "summary": "Scanned PDF with no readable text layer — needs manual review.",
                    "extracted": {}}
        if mime.startswith("text/"):
            return await _text_classify(content.decode("utf-8", "ignore"), file_name)
    except Exception:  # noqa: BLE001 - best-effort; fall through to the review result
        logger.exception("document classification failed for %s", file_name)
    return {"category": "other", "confidence": 0.0,
            "summary": "Could not classify automatically — needs manual review.", "extracted": {}}
