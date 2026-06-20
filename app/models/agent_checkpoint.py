"""LangGraph state snapshots so the agent can resume exactly where it left off."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class AgentCheckpoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_checkpoints"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Denormalized from the thread for uniform tenant/lead RLS.
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[dict | None] = mapped_column(JSONB)
    parent_checkpoint_id: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        Index("ix_agent_checkpoints_thread_id", "agent_thread_id"),
        Index("ix_agent_checkpoints_lead_id", "lead_id"),
        Index("ix_agent_checkpoints_organization_id", "organization_id"),
    )
