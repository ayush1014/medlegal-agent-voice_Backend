"""Reminders / next actions that power smart follow-ups (staff- or agent-created).
Not always tied to a lead (lead_id nullable)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import CREATED_BY_TYPES, TASK_STATUSES, sql_in


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tasks"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE")
    )
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="open"
    )
    created_by: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="user"
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(sql_in("status", TASK_STATUSES), name="ck_tasks_status"),
        CheckConstraint(
            sql_in("created_by", CREATED_BY_TYPES), name="ck_tasks_created_by"
        ),
        Index("ix_tasks_lead_id", "lead_id"),
        Index("ix_tasks_organization_id", "organization_id"),
        Index("ix_tasks_org_assigned", "organization_id", "assigned_user_id"),
    )
