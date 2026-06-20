"""One transcript per intake call — the narrative source for the AI."""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import TRANSCRIPT_STATUSES, sql_in


class IntakeTranscript(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "intake_transcripts"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
    )
    voice_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "voice_calls.id",
            ondelete="SET NULL",
            name="fk_intake_transcripts_voice_call_id",
        ),
    )
    language: Mapped[str | None] = mapped_column(String(16))
    full_text: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    # What the agent parsed: injuries, dates, insurance, etc.
    extracted_fields: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="in_progress"
    )

    __table_args__ = (
        CheckConstraint(
            sql_in("status", TRANSCRIPT_STATUSES), name="ck_intake_transcripts_status"
        ),
        Index("ix_intake_transcripts_lead_id", "lead_id"),
        Index("ix_intake_transcripts_organization_id", "organization_id"),
    )
