"""Twilio call records. May exist before a lead is created (lead_id nullable)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import MESSAGE_DIRECTIONS, sql_in


class VoiceCall(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "voice_calls"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL")
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    from_e164: Mapped[str | None] = mapped_column(String(20))
    to_e164: Mapped[str | None] = mapped_column(String(20))
    # Twilio call SID — unique provider reference.
    provider_sid: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Open vocabulary (Twilio call statuses: queued, ringing, in-progress, ...).
    status: Mapped[str | None] = mapped_column(String(32))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    recording_url: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            sql_in("direction", MESSAGE_DIRECTIONS), name="ck_voice_calls_direction"
        ),
        Index("ix_voice_calls_lead_id", "lead_id"),
        Index("ix_voice_calls_organization_id", "organization_id"),
    )
