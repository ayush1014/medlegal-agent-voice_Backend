"""Firm staff who log into the admin dashboard."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import USER_ROLES, sql_in


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Stored lowercased by the app; unique per organization (see __table_args__).
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # E.164, unique per organization (see __table_args__). Firm-branded login
    # resolves the org before credentials, so per-org is unambiguous. Normalized
    # to E.164 by the app on write.
    phone: Mapped[str | None] = mapped_column(String(20))
    # Nullable until the auth PRD wires credential issuance (Argon2id).
    password_hash: Mapped[str | None] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(255))
    # Invariant (current scope): every firm user is created as 'owner'/'admin'.
    # We mint no other staff tiers and gate no access logic on them; the column
    # keeps the full user_role vocabulary so tiers can be added later w/o a migration.
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    mfa_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_users_org_email"),
        CheckConstraint(sql_in("role", USER_ROLES), name="ck_users_role"),
        Index("ix_users_organization_id", "organization_id"),
        # Per-org phone uniqueness for firm-branded phone+OTP login.
        Index(
            "uq_users_org_phone",
            "organization_id",
            "phone",
            unique=True,
            postgresql_where=text("phone IS NOT NULL"),
        ),
    )
