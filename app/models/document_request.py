"""The ask — which documents we need from the client."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    DOCUMENT_REQUEST_STATUSES,
    MESSAGE_CHANNELS,
    REQUESTABLE_DOCUMENTS,
    sql_in,
)


class DocumentRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "document_requests"

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
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="Pending"
    )
    requested_via: Mapped[str | None] = mapped_column(String(16))
    requested_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    due_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            sql_in("document_type", REQUESTABLE_DOCUMENTS),
            name="ck_document_requests_document_type",
        ),
        CheckConstraint(
            sql_in("status", DOCUMENT_REQUEST_STATUSES),
            name="ck_document_requests_status",
        ),
        CheckConstraint(
            f"requested_via IS NULL OR {sql_in('requested_via', MESSAGE_CHANNELS)}",
            name="ck_document_requests_requested_via",
        ),
        Index("ix_document_requests_lead_id", "lead_id"),
        Index("ix_document_requests_organization_id", "organization_id"),
    )
