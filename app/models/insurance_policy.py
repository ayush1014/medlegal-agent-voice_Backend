"""The payout cap. Captures both claimant and at-fault coverage."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import POLICY_KINDS, POLICY_PARTY_ROLES, sql_in


class InsurancePolicy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "insurance_policies"

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
    party_role: Mapped[str] = mapped_column(String(16), nullable=False)
    carrier_name: Mapped[str | None] = mapped_column(String(255))
    policy_kind: Mapped[str | None] = mapped_column(String(16))
    policy_number: Mapped[str | None] = mapped_column(String(80))
    coverage_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    claim_number: Mapped[str | None] = mapped_column(String(80))
    adjuster_contact: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            sql_in("party_role", POLICY_PARTY_ROLES),
            name="ck_insurance_policies_party_role",
        ),
        CheckConstraint(
            f"policy_kind IS NULL OR {sql_in('policy_kind', POLICY_KINDS)}",
            name="ck_insurance_policies_policy_kind",
        ),
        Index("ix_insurance_policies_lead_id", "lead_id"),
        Index("ix_insurance_policies_organization_id", "organization_id"),
    )
