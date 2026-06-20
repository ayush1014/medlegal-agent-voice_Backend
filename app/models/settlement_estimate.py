"""Settlement estimates — append-only. Every estimate is a new row so we can
audit how value changed and why (no UPDATE/DELETE; enforced via grants + RLS)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import CREATED_BY_TYPES, SETTLEMENT_CONFIDENCES, sql_in


class SettlementEstimate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "settlement_estimates"

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
    low: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    expected: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    high: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    confidence: Mapped[str | None] = mapped_column(String(16))
    pain_multiplier: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    # The damages/injuries/policy figures the estimate was computed from.
    inputs_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(80))
    reasoning: Mapped[str | None] = mapped_column(Text)
    created_by_type: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="system"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint(
            f"confidence IS NULL OR {sql_in('confidence', SETTLEMENT_CONFIDENCES)}",
            name="ck_settlement_estimates_confidence",
        ),
        CheckConstraint(
            sql_in("created_by_type", CREATED_BY_TYPES),
            name="ck_settlement_estimates_created_by_type",
        ),
        Index("ix_settlement_estimates_lead_created", "lead_id", text("created_at DESC")),
        Index("ix_settlement_estimates_organization_id", "organization_id"),
    )
