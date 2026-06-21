"""Wipe a firm's lead/client/call data for a fresh start — KEEPS the firm itself,
its staff users, and its phone numbers, so you can keep logging in and receiving calls.

Deletes (scoped to the org): leads (cascades all case children — incidents, injuries,
treatments, insurance, parties, damages, scores, settlements, documents, retainers,
signature events, conversations/messages, tasks, transcripts/segments, knowledge
chunks, KG nodes/edges, agent threads/events, short links, AND client_accounts via
the lead CASCADE), voice_calls, outbox_events, and any stray client_accounts/short
links. Also clears the global webhook idempotency log so reused test SIDs reprocess.

    python -m scripts.reset_firm_data            # firm with +16076956595 (else slug 'demo')
    python -m scripts.reset_firm_data --slug acme
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import _build_async_url

DEFAULT_PHONE = "+16076956595"


async def main(slug: str | None, phone: str) -> None:
    engine = create_async_engine(_build_async_url(settings.database_url), poolclass=NullPool)
    try:
        async with engine.begin() as c:
            org = None
            if slug:
                org = (await c.execute(text("SELECT id FROM organizations WHERE slug=:s"), {"s": slug})).scalar()
            if org is None:
                org = (await c.execute(text("SELECT organization_id FROM phone_numbers WHERE e164=:p"),
                                       {"p": phone})).scalar()
            if org is None:
                org = (await c.execute(text("SELECT id FROM organizations WHERE slug='demo'"))).scalar()
            if org is None:
                print("No matching firm found."); return

            name = (await c.execute(text("SELECT name FROM organizations WHERE id=:o"), {"o": org})).scalar()

            async def count(tbl: str) -> int:
                return (await c.execute(text(f"SELECT count(*) FROM {tbl} WHERE organization_id=:o"),
                                        {"o": org})).scalar()

            before = {t: await count(t) for t in ("leads", "voice_calls", "client_accounts", "messages")}
            print(f"Firm: {name} ({org})")
            print("  before:", before)

            # leads CASCADE removes all case children + client_accounts (lead_id CASCADE).
            await c.execute(text("DELETE FROM leads WHERE organization_id=:o"), {"o": org})
            await c.execute(text("DELETE FROM voice_calls WHERE organization_id=:o"), {"o": org})
            await c.execute(text("DELETE FROM outbox_events WHERE organization_id=:o"), {"o": org})
            await c.execute(text("DELETE FROM client_accounts WHERE organization_id=:o"), {"o": org})
            await c.execute(text("DELETE FROM short_links WHERE organization_id=:o"), {"o": org})
            await c.execute(text("DELETE FROM webhook_events"))  # global idempotency reset

            after = {t: await count(t) for t in ("leads", "voice_calls", "client_accounts", "messages")}
            kept_staff = (await c.execute(text("SELECT count(*) FROM users WHERE organization_id=:o"),
                                          {"o": org})).scalar()
            kept_numbers = await count("phone_numbers")
            print("  after :", after)
            print(f"  KEPT  : staff users={kept_staff}, phone_numbers={kept_numbers}, firm intact")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default=None)
    ap.add_argument("--phone", default=DEFAULT_PHONE)
    args = ap.parse_args()
    asyncio.run(main(args.slug, args.phone))
