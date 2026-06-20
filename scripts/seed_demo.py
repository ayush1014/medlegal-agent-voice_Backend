"""Seed a demo firm + admin + a few leads so the dashboard shows real data.

Dev convenience (idempotent: drops and recreates the 'demo' org). Uses the owner
connection to bypass RLS for seeding.

    python -m scripts.seed_demo
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.database import _build_async_url
from app.security.passwords import hash_password

SLUG = "demo"
ADMIN_EMAIL = "demo@example.com"
ADMIN_PASSWORD = "demodemo123"
ADMIN_PHONE = "+16075550100"

LEADS = [
    ("John Doe", "+14045551234", "john@example.com", "Auto Accident", "Atlanta, GA",
     "2026-06-12", "Qualified", 88, "Hot", 35000, "Docs Requested", "Not Ready", 2),
    ("Maria Smith", "+16465559988", "maria@example.com", "Slip and Fall", "Brooklyn, NY",
     "2026-05-30", "Needs Review", 72, "Warm", 22000, "Needs Review", "Not Ready", 3),
    ("Robert Johnson", "+19175556789", "robert@example.com", "Dog Bite", "Queens, NY",
     "2026-06-03", "Qualified", 81, "Hot", 30000, "Qualified", "Ready", 1),
    ("Aisha Khan", "+12125553322", "aisha@example.com", "Motorcycle Accident", "Newark, NJ",
     "2026-06-08", "Qualified", 92, "Hot", 95000, "Retainer Sent", "Sent", 0),
    ("Grace Liu", "+14155552210", "grace@example.com", "Truck Accident", "Oakland, CA",
     "2026-04-22", "Qualified", 96, "Hot", 240000, "Signed", "Signed", 0),
]


async def seed() -> None:
    engine = create_async_engine(_build_async_url(settings.database_url))
    org_id = uuid.uuid4()
    try:
        async with engine.begin() as c:
            await c.execute(text("DELETE FROM organizations WHERE slug = :s"), {"s": SLUG})
            await c.execute(
                text("INSERT INTO organizations (id, name, slug, intake_phone_e164) "
                     "VALUES (:id,'Demo PI Firm',:s,:p)"),
                {"id": org_id, "s": SLUG, "p": ADMIN_PHONE},
            )
            await c.execute(
                text("INSERT INTO users (organization_id, email, phone, password_hash, role, full_name) "
                     "VALUES (:o,:e,:p,:h,'owner','Demo Admin')"),
                {"o": org_id, "e": ADMIN_EMAIL, "p": ADMIN_PHONE, "h": hash_password(ADMIN_PASSWORD)},
            )
            for (name, phone, email, ct, loc, idate, qual, score, temp, settle,
                 pipe, retainer, missing) in LEADS:
                lead_id = uuid.uuid4()
                await c.execute(
                    text("INSERT INTO leads (id, organization_id, full_name, phone, email, case_type, "
                         "qualification_status, lead_score, lead_temperature, settlement_expected, "
                         "pipeline_status, retainer_status, missing_documents, source) "
                         "VALUES (:id,:o,:n,:ph,:em,:ct,:q,:sc,:tp,:se,:pi,:re,:mi,'web')"),
                    {"id": lead_id, "o": org_id, "n": name, "ph": phone, "em": email, "ct": ct,
                     "q": qual, "sc": score, "tp": temp, "se": Decimal(settle), "pi": pipe,
                     "re": retainer, "mi": missing},
                )
                await c.execute(
                    text("INSERT INTO incidents (organization_id, lead_id, incident_date, location_text) "
                         "VALUES (:o,:l,:d,:loc)"),
                    {"o": org_id, "l": lead_id, "d": date.fromisoformat(idate), "loc": loc},
                )
    finally:
        await engine.dispose()

    print(f"Seeded firm slug='{SLUG}' with {len(LEADS)} leads.")
    print(f"Admin login → email={ADMIN_EMAIL}  password={ADMIN_PASSWORD}  (X-Org-Slug: {SLUG})")


if __name__ == "__main__":
    asyncio.run(seed())
