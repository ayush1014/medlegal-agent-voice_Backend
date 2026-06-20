"""Twilio numbers provisioned per firm (collected during onboarding)."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class PhoneNumber(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "phone_numbers"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    e164: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'twilio'")
    )
    provider_sid: Mapped[str | None] = mapped_column(String(64))
    capabilities: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    is_primary: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )

    __table_args__ = (
        Index("ix_phone_numbers_organization_id", "organization_id"),
    )
