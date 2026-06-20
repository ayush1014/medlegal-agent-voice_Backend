"""The actual uploaded file (separate from the request — one request may yield
several files, and clients send unrequested files too).

Files live in GCP Storage; the DB holds metadata + a storage reference. Access
is gated on virus-scan status.
"""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import DOCUMENT_SCAN_STATUSES, DOCUMENT_UPLOADED_BY, sql_in


class Document(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "documents"

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
    document_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_requests.id", ondelete="SET NULL")
    )
    file_name: Mapped[str | None] = mapped_column(String(512))
    # GCS object path / reference; the API serves time-limited signed URLs.
    storage_url: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    uploaded_by: Mapped[str | None] = mapped_column(String(16))
    scan_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )

    __table_args__ = (
        CheckConstraint(
            f"uploaded_by IS NULL OR {sql_in('uploaded_by', DOCUMENT_UPLOADED_BY)}",
            name="ck_documents_uploaded_by",
        ),
        CheckConstraint(
            sql_in("scan_status", DOCUMENT_SCAN_STATUSES), name="ck_documents_scan_status"
        ),
        Index(
            "ix_documents_lead_id",
            "lead_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_documents_organization_id", "organization_id"),
        Index("ix_documents_request_id", "document_request_id"),
    )
