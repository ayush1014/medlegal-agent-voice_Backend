"""The Letter of Representation / engagement agreement (one per lead)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import RETAINER_STATUSES, sql_in


class Retainer(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "retainers"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="Not Ready"
    )
    template_id: Mapped[str | None] = mapped_column(String(80))
    document_url: Mapped[str | None] = mapped_column(Text)
    # Name the client typed when e-signing (with signature_events for the full audit trail).
    signer_name: Mapped[str | None] = mapped_column(String(255))
    # Internal mock for now; clean seam for DocuSign/Dropbox Sign later.
    esign_provider: Mapped[str | None] = mapped_column(String(40))
    esign_envelope_id: Mapped[str | None] = mapped_column(String(128))
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    viewed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    signed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            sql_in("status", RETAINER_STATUSES), name="ck_retainers_status"
        ),
        Index("ix_retainers_organization_id", "organization_id"),
    )
