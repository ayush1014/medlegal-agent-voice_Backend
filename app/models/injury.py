"""One row per injury. Severity/permanence drive the pain multiplier."""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import INJURY_SEVERITIES, sql_in


class Injury(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "injuries"

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
    body_part: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str | None] = mapped_column(String(16))
    is_permanent: Mapped[bool | None] = mapped_column()
    requires_surgery: Mapped[bool | None] = mapped_column()

    __table_args__ = (
        CheckConstraint(
            f"severity IS NULL OR {sql_in('severity', INJURY_SEVERITIES)}",
            name="ck_injuries_severity",
        ),
        Index("ix_injuries_lead_id", "lead_id"),
        Index("ix_injuries_organization_id", "organization_id"),
    )
