"""The lead — the spine of the product. Detailed facts live in child tables;
this row keeps only the headline values the list/dashboard reads, so those
views stay fast."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    CASE_TYPES,
    LEAD_SOURCES,
    LEAD_TEMPERATURES,
    PIPELINE_STATUSES,
    QUALIFICATION_STATUSES,
    RETAINER_STATUSES,
    sql_in,
)


class Lead(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "leads"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )

    # --- Contact ---
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    preferred_contact_method: Mapped[str | None] = mapped_column(String(32))
    best_time_to_contact: Mapped[str | None] = mapped_column(String(64))
    # Captured at intake; used to verify a returning caller (name + DOB).
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    address: Mapped[str | None] = mapped_column(String(512))

    # --- Work & income (feeds lost-earnings in settlement) ---
    occupation: Mapped[str | None] = mapped_column(String(255))
    employer: Mapped[str | None] = mapped_column(String(255))
    employment_status: Mapped[str | None] = mapped_column(String(64))
    annual_income: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # --- Case header (denormalized for fast lists) ---
    case_type: Mapped[str] = mapped_column(String(40), nullable=False)
    pipeline_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'New Lead'")
    )
    qualification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'Needs Review'")
    )
    lead_temperature: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'Low'")
    )
    lead_score: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    settlement_expected: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    retainer_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'Not Ready'")
    )

    # --- Rollups (kept in sync from child tables by the service layer) ---
    missing_documents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_follow_up_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # Short, client-safe summary (leads list + client portal).
    ai_summary: Mapped[str | None] = mapped_column(Text)
    # Richer, sectioned attorney brief (internal lead detail only). See intake_pipeline.
    case_brief: Mapped[dict | None] = mapped_column(JSONB)
    # Whether the client already has an attorney (latest call wins). Authoritative for
    # the funnel; ai_summary keeps a human-readable note too.
    has_attorney: Mapped[bool | None] = mapped_column(Boolean)

    # --- Ownership / source ---
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'inbound_call'")
    )

    __table_args__ = (
        CheckConstraint(sql_in("case_type", CASE_TYPES), name="ck_leads_case_type"),
        CheckConstraint(
            sql_in("pipeline_status", PIPELINE_STATUSES), name="ck_leads_pipeline_status"
        ),
        CheckConstraint(
            sql_in("qualification_status", QUALIFICATION_STATUSES),
            name="ck_leads_qualification_status",
        ),
        CheckConstraint(
            sql_in("lead_temperature", LEAD_TEMPERATURES), name="ck_leads_temperature"
        ),
        CheckConstraint(
            sql_in("retainer_status", RETAINER_STATUSES), name="ck_leads_retainer_status"
        ),
        CheckConstraint(sql_in("source", LEAD_SOURCES), name="ck_leads_source"),
        CheckConstraint(
            "lead_score >= 0 AND lead_score <= 100", name="ck_leads_score_range"
        ),
        # Dashboard list/sort paths (tenant-first), skipping soft-deleted rows.
        Index(
            "ix_leads_org_updated",
            "organization_id",
            text("updated_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_leads_org_score",
            "organization_id",
            text("lead_score DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_leads_org_pipeline", "organization_id", "pipeline_status"),
        Index("ix_leads_org_assigned", "organization_id", "assigned_user_id"),
    )
