"""Structured accident facts for a lead."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class Incident(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "incidents"

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
    incident_date: Mapped[date | None] = mapped_column(Date)
    location_text: Mapped[str | None] = mapped_column(String(512))
    lat: Mapped[float | None] = mapped_column(Numeric(9, 6))
    lng: Mapped[float | None] = mapped_column(Numeric(9, 6))
    description: Mapped[str | None] = mapped_column(Text)
    police_report_available: Mapped[bool | None] = mapped_column()
    police_report_number: Mapped[str | None] = mapped_column(String(64))
    fault_narrative: Mapped[str | None] = mapped_column(Text)
    comparative_negligence_pct: Mapped[int | None] = mapped_column(Integer)
    statute_of_limitations_date: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        Index("ix_incidents_lead_id", "lead_id"),
        Index("ix_incidents_organization_id", "organization_id"),
    )
