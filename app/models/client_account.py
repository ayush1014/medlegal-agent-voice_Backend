"""The injured person's login for the user (client) dashboard. Bound 1:1 to a lead."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class ClientAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "client_accounts"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 1:1 with a lead — a client account can access only its linked lead.
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(20))
    # Nullable: portal login may be magic-link / OTP rather than password.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        Index("ix_client_accounts_organization_id", "organization_id"),
        # Per-org login lookups for "sign in by phone or email". Partial-unique so
        # one client account per (firm, email) / (firm, phone). Email is lowercased
        # and phone is E.164-normalized by the app on write.
        Index(
            "uq_client_accounts_org_email",
            "organization_id",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        Index(
            "uq_client_accounts_org_phone",
            "organization_id",
            "phone",
            unique=True,
            postgresql_where=text("phone IS NOT NULL"),
        ),
    )
