"""Every SMS / WhatsApp / email / voice follow-up, inbound and outbound.

This is the single comms log — "follow-up messages" and "document-request SMS"
are just rows filtered by `purpose`, not separate tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    MESSAGE_CHANNELS,
    MESSAGE_DIRECTIONS,
    MESSAGE_PURPOSES,
    MESSAGE_STATUSES,
    sql_in,
)


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    media: Mapped[dict | None] = mapped_column(JSONB)
    purpose: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="general"
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="queued"
    )
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(sql_in("channel", MESSAGE_CHANNELS), name="ck_messages_channel"),
        CheckConstraint(
            sql_in("direction", MESSAGE_DIRECTIONS), name="ck_messages_direction"
        ),
        CheckConstraint(sql_in("purpose", MESSAGE_PURPOSES), name="ck_messages_purpose"),
        CheckConstraint(sql_in("status", MESSAGE_STATUSES), name="ck_messages_status"),
        Index("ix_messages_conversation_id", "conversation_id"),
        Index("ix_messages_lead_id", "lead_id"),
        Index("ix_messages_organization_id", "organization_id"),
    )
