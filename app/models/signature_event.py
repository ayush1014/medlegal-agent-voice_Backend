"""The e-sign proof trail (legally important) — append-only."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import INET, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import SIGNATURE_ACTORS, SIGNATURE_EVENT_TYPES, sql_in


class SignatureEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "signature_events"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    retainer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("retainers.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Denormalized from the retainer for uniform tenant/lead RLS.
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
    )
    event: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(16))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    provider_payload: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            sql_in("event", SIGNATURE_EVENT_TYPES), name="ck_signature_events_event"
        ),
        CheckConstraint(
            f"actor IS NULL OR {sql_in('actor', SIGNATURE_ACTORS)}",
            name="ck_signature_events_actor",
        ),
        Index("ix_signature_events_retainer_id", "retainer_id"),
        Index("ix_signature_events_lead_id", "lead_id"),
        Index("ix_signature_events_organization_id", "organization_id"),
    )
