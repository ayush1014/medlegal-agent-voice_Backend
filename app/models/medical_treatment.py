"""Treatment timeline — continuity vs. gaps is a value signal."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class MedicalTreatment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "medical_treatments"

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
    provider_name: Mapped[str | None] = mapped_column(String(255))
    # Open vocabulary (ER, urgent care, PT, chiropractor, surgeon, ...).
    provider_type: Mapped[str | None] = mapped_column(String(64))
    treatment_type: Mapped[str | None] = mapped_column(String(120))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    is_ongoing: Mapped[bool | None] = mapped_column()
    billed_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    __table_args__ = (
        Index("ix_medical_treatments_lead_id", "lead_id"),
        Index("ix_medical_treatments_organization_id", "organization_id"),
    )
