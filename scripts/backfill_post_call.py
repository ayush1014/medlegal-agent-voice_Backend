"""Backfill the post-call pipeline for leads whose call completed but were never
processed (the shutdown-callback bug killed extraction before it persisted).

Targets leads still at pipeline 'Intake Started' with NO lead_score, using their
most recent transcript that has saved text. Idempotent enough for a one-shot
recovery; run once after deploying the decoupled pipeline.

    python -m scripts.backfill_post_call            # all firms
    python -m scripts.backfill_post_call --slug demo
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import _build_async_url
from app.services.intake_pipeline import run_post_call_pipeline

_CANDIDATES = """
SELECT DISTINCT ON (t.lead_id)
       t.lead_id, t.id AS transcript_id, t.full_text, t.voice_call_id,
       l.organization_id, l.phone
FROM intake_transcripts t
JOIN leads l ON l.id = t.lead_id
WHERE t.status IN ('complete', 'failed')
  AND t.full_text IS NOT NULL AND length(t.full_text) > 0
  AND l.pipeline_status = 'Intake Started'
  AND l.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM lead_scores s WHERE s.lead_id = l.id)
  AND (CAST(:org AS uuid) IS NULL OR l.organization_id = CAST(:org AS uuid))
ORDER BY t.lead_id, t.created_at DESC
"""


async def main(slug: str | None) -> None:
    engine = create_async_engine(_build_async_url(settings.database_url), poolclass=NullPool)
    try:
        org = None
        if slug:
            async with engine.connect() as c:
                org = (await c.execute(text("SELECT id FROM organizations WHERE slug=:s"), {"s": slug})).scalar()
            if org is None:
                print(f"No firm with slug {slug!r}."); return
        async with engine.connect() as c:
            rows = (await c.execute(text(_CANDIDATES), {"org": org})).all()

        print(f"Found {len(rows)} lead(s) to backfill.")
        ok = fail = 0
        for r in rows:
            try:
                # caller_phone=None so the backfill never re-sends a welcome SMS.
                await run_post_call_pipeline(
                    organization_id=r.organization_id, lead_id=r.lead_id,
                    transcript_text=r.full_text, transcript_id=r.transcript_id,
                    voice_call_id=r.voice_call_id, caller_phone=None,
                )
                ok += 1
                print(f"  ✓ {r.lead_id}")
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"  ✗ {r.lead_id}: {e}")
        print(f"Done. backfilled={ok} failed={fail}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default=None)
    args = ap.parse_args()
    asyncio.run(main(args.slug))
