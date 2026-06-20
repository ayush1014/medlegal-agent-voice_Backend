"""Per-case knowledge-graph entities (person, injury, provider, insurer, ...)."""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import KG_NODE_TYPES, sql_in


class KgNode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "kg_nodes"

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
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str | None] = mapped_column(String(255))
    props: Mapped[dict | None] = mapped_column(JSONB)
    source_ref: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(sql_in("node_type", KG_NODE_TYPES), name="ck_kg_nodes_node_type"),
        Index("ix_kg_nodes_lead_id", "lead_id"),
        Index("ix_kg_nodes_organization_id", "organization_id"),
    )
