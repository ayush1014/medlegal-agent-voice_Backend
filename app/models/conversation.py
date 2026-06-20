"""A messaging thread with a lead/client across one or more channels."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import MESSAGE_CHANNELS, sql_in


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"

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
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(255))
    last_message_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    is_open: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))

    __table_args__ = (
        CheckConstraint(
            sql_in("channel", MESSAGE_CHANNELS), name="ck_conversations_channel"
        ),
        Index("ix_conversations_lead_id", "lead_id"),
        Index("ix_conversations_organization_id", "organization_id"),
    )
