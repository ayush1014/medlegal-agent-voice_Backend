"""Itemized economic damages — the inputs to the settlement math."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import DAMAGE_CATEGORIES, DAMAGE_SOURCES, sql_in


class Damage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "damages"

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
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    is_estimated: Mapped[bool | None] = mapped_column()
    source: Mapped[str | None] = mapped_column(String(16))

    __table_args__ = (
        CheckConstraint(
            sql_in("category", DAMAGE_CATEGORIES), name="ck_damages_category"
        ),
        CheckConstraint(
            f"source IS NULL OR {sql_in('source', DAMAGE_SOURCES)}",
            name="ck_damages_source",
        ),
        Index("ix_damages_lead_id", "lead_id"),
        Index("ix_damages_organization_id", "organization_id"),
    )
