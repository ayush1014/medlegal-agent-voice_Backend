"""FastAPI application entrypoint.

Run locally:
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config import settings
from app.database import engine
from app.services import lead_intelligence  # noqa: F401 - registers outbox handlers

logger = logging.getLogger("medlegal.scheduler")


async def _followups_scheduler() -> None:
    """In-app follow-up tick (single-instance deploys; cron is canonical otherwise)."""
    from app.jobs.followups import run_all_orgs

    while True:
        await asyncio.sleep(settings.followups_interval_seconds)
        try:
            result = await run_all_orgs()
            logger.info("followups tick: %s", result)
        except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
            logger.exception("followups tick failed")


async def _post_call_worker() -> None:
    """Drain `call.ended` events (heavy post-call pipeline) off the voice worker.
    Runs in the always-alive API process, so a hangup never kills extraction."""
    from app.jobs.post_call import process_pending_call_ended
    from app.services import outbox_publisher

    while True:
        try:
            res = await process_pending_call_ended()
            if res.get("processed") or res.get("failed") or res.get("retried"):
                logger.info("post-call tick: %s", res)
            await outbox_publisher.dispatch_pending()  # intake.completed → scoring + retries
        except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
            logger.exception("post-call tick failed")
        await asyncio.sleep(settings.post_call_interval_seconds)


async def _email_inbound_worker() -> None:
    """Ingest client document replies from the firm inbox (Gmail IMAP)."""
    from app.jobs.email_inbound import poll_inbound

    while True:
        try:
            res = await poll_inbound()
            if res.get("emails") or res.get("files"):
                logger.info("email inbound tick: %s", res)
        except Exception:  # noqa: BLE001 - a bad tick must not kill the loop
            logger.exception("email inbound tick failed")
        await asyncio.sleep(settings.email_poll_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup / shutdown hooks."""
    tasks: list[asyncio.Task] = []
    if settings.post_call_worker_enabled:
        tasks.append(asyncio.create_task(_post_call_worker()))
    if settings.email_inbound_enabled and settings.email_enabled:
        tasks.append(asyncio.create_task(_email_inbound_worker()))
    if settings.followups_scheduler_enabled:
        tasks.append(asyncio.create_task(_followups_scheduler()))
    yield
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    # Dispose the connection pool on shutdown.
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        debug=settings.debug,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # All routes live under the API prefix (e.g. /api/health).
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    # Short links (/u/{code}) mounted at root so texted URLs stay tiny + clickable.
    from app.api.routes import links

    app.include_router(links.router)

    @app.get("/", tags=["root"])
    async def root() -> dict[str, str]:
        return {"name": settings.app_name, "docs": "/docs"}

    return app


app = create_app()
