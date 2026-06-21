"""Follow-up + outbox sweep across all firms (the cron job).

Run from system cron / a scheduler:
    python -m app.jobs.followups

Or enable the in-app scheduler with FOLLOWUPS_SCHEDULER_ENABLED=true (single-instance
deploys only). Each tick: (1) sweeps the outbox so any unprocessed intake.completed
events get scored, then (2) advances + nudges every firm's leads. Both steps are
idempotent and per-org isolated, so a failing firm never blocks the others.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import _build_async_url
from app.services import followup_service, outbox_publisher

# Import registers outbox handlers (intake.completed -> scoring/qualification/settlement).
from app.services import lead_intelligence  # noqa: F401

logger = logging.getLogger("medlegal.followups")


async def _list_org_ids() -> list[uuid.UUID]:
    engine = create_async_engine(_build_async_url(settings.database_url), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT id FROM organizations"))).all()
        return [r.id for r in rows]
    finally:
        await engine.dispose()


async def run_all_orgs() -> dict:
    """One full tick across all firms. Returns aggregate counts."""
    totals = {"orgs": 0, "events_published": 0, "docs_requested": 0,
              "retainers_sent": 0, "doc_nudges": 0, "retainer_nudges": 0, "errors": 0}

    # 1. Outbox sweep (catches any intake.completed not dispatched inline).
    try:
        swept = await outbox_publisher.dispatch_pending()
        totals["events_published"] += swept.get("published", 0)
    except Exception:  # noqa: BLE001
        totals["errors"] += 1

    # 2. Per-org follow-up advancement.
    for org in await _list_org_ids():
        totals["orgs"] += 1
        try:
            c = await followup_service.run_followups(org)
            for k in ("docs_requested", "retainers_sent", "doc_nudges", "retainer_nudges"):
                totals[k] += c.get(k, 0)
        except Exception:  # noqa: BLE001 - isolate per firm
            logger.exception("followups failed for org %s", org)
            totals["errors"] += 1
    return totals


def main() -> None:
    result = asyncio.run(run_all_orgs())
    print("followups tick:", result)


if __name__ == "__main__":
    main()
