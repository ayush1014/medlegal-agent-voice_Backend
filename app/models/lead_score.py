"""Lead scoring history — append-only. Carries the reasoning factors the
frontend shows (no UPDATE/DELETE; enforced via grants + RLS)."""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    CREATED_BY_TYPES,
    LEAD_TEMPERATURES,
    QUALIFICATION_STATUSES,
    sql_in,
)


class LeadScore(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "lead_scores"

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
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    temperature: Mapped[str | None] = mapped_column(String(16))
    qualification_status: Mapped[str | None] = mapped_column(String(32))
    qualification_reason: Mapped[str | None] = mapped_column(Text)
    # Array of factor strings (frontend's scoreReasoning[]).
    reasoning: Mapped[list | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(80))
    created_by_type: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="system"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint("score >= 0 AND score <= 100", name="ck_lead_scores_range"),
        CheckConstraint(
            f"temperature IS NULL OR {sql_in('temperature', LEAD_TEMPERATURES)}",
            name="ck_lead_scores_temperature",
        ),
        CheckConstraint(
            f"qualification_status IS NULL OR "
            f"{sql_in('qualification_status', QUALIFICATION_STATUSES)}",
            name="ck_lead_scores_qualification_status",
        ),
        CheckConstraint(
            sql_in("created_by_type", CREATED_BY_TYPES),
            name="ck_lead_scores_created_by_type",
        ),
        Index("ix_lead_scores_lead_created", "lead_id", text("created_at DESC")),
        Index("ix_lead_scores_organization_id", "organization_id"),
    )
