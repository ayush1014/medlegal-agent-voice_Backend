"""Inbound webhook idempotency (Twilio / e-sign).

Infrastructure table: deduped by provider event id BEFORE the tenant is known,
so it carries no organization_id and no RLS — it is touched only by the system
ingestion path.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import WEBHOOK_STATUSES, sql_in


class WebhookEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "webhook_events"

    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    event_type: Mapped[str | None] = mapped_column(String(80))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    processed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="received"
    )

    __table_args__ = (
        CheckConstraint(
            sql_in("status", WEBHOOK_STATUSES), name="ck_webhook_events_status"
        ),
        Index("ix_webhook_events_provider", "provider"),
    )
