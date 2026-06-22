"""Document classifier routing + normalization (document AI, scoring step #4).

Pure unit tests — the model calls are monkeypatched, so these are fast and offline.
They lock in: images go to vision, text-layer PDFs go to text, scanned PDFs route to
review, and malformed model output is normalized rather than trusted.
"""
from __future__ import annotations

import pytest

from app.services import document_ai


def test_clean_normalizes_bad_output():
    out = document_ai._clean({"category": "totally-bogus", "confidence": 5, "summary": "  hi  ",
                              "extracted": "not-a-dict"})
    assert out["category"] == "other"          # unknown -> other
    assert out["confidence"] == 1.0            # clamped to [0,1]
    assert out["summary"] == "hi"
    assert out["extracted"] == {}              # non-dict -> {}


def test_clean_keeps_valid_output():
    out = document_ai._clean({"category": "medical_bill", "confidence": 0.9,
                              "summary": "bill", "extracted": {"billed_amount": 100}})
    assert out == {"category": "medical_bill", "confidence": 0.9, "summary": "bill",
                   "extracted": {"billed_amount": 100}}


@pytest.mark.asyncio
async def test_classify_image_routes_to_vision(monkeypatch):
    seen = {}

    async def fake_vision(content, mime, name):
        seen["vision"] = (mime, name)
        return {"category": "accident_photo", "confidence": 0.8, "summary": "", "extracted": {}}

    monkeypatch.setattr(document_ai, "_vision_classify", fake_vision)
    r = await document_ai.classify("crash.jpg", b"\x89PNG", "image/jpeg")
    assert r["category"] == "accident_photo" and seen["vision"][0] == "image/jpeg"


@pytest.mark.asyncio
async def test_classify_text_pdf_routes_to_text(monkeypatch):
    monkeypatch.setattr(document_ai, "_pdf_text", lambda c: "POLICE REPORT incident number 12345 " * 5)

    async def fake_text(text_content, name):
        return {"category": "police_report", "confidence": 0.9, "summary": "", "extracted": {}}

    monkeypatch.setattr(document_ai, "_text_classify", fake_text)
    r = await document_ai.classify("report.pdf", b"%PDF-1.4", "application/pdf")
    assert r["category"] == "police_report"


@pytest.mark.asyncio
async def test_classify_scanned_pdf_routes_to_review(monkeypatch):
    monkeypatch.setattr(document_ai, "_pdf_text", lambda c: "")  # no text layer
    r = await document_ai.classify("scan.pdf", b"%PDF-1.4", "application/pdf")
    assert r["category"] == "other" and r["confidence"] == 0.0 and "review" in r["summary"].lower()


def test_category_keywords_cover_default_checklist():
    # Every category we can auto-match has at least one requirement keyword.
    for cat in ("medical_bill", "medical_record", "police_report", "insurance_dec",
                "vehicle_damage", "accident_photo", "injury_photo"):
        assert document_ai.CATEGORY_REQUIREMENT_KEYWORDS.get(cat)
