"""Knowledge-graph relationships — the case's "absolute memory".

e.g. (client)-[injured_in]->(incident), (client)-[treated_by]->(provider),
(at_fault_driver)-[insured_by]->(insurer). `relation` is open vocabulary.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, NUMERIC, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class KgEdge(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "kg_edges"

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
    subject_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kg_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    relation: Mapped[str] = mapped_column(String(64), nullable=False)
    object_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kg_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    props: Mapped[dict | None] = mapped_column(JSONB)
    confidence: Mapped[Decimal | None] = mapped_column(NUMERIC(4, 3))

    __table_args__ = (
        Index("ix_kg_edges_lead_id", "lead_id"),
        Index("ix_kg_edges_organization_id", "organization_id"),
        Index("ix_kg_edges_subject_node_id", "subject_node_id"),
        Index("ix_kg_edges_object_node_id", "object_node_id"),
    )
