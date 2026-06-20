"""The single Hybrid + BM25 RAG table.

Everything embeddable (transcripts, documents, notes, settlement reasoning,
medical summaries, messages, incidents) becomes a chunk here, so retrieval is
uniform. Hybrid search = vector (HNSW over halfvec) + keyword (GIN over a
generated tsvector), fused with Reciprocal Rank Fusion in the service layer.

The HNSW (embedding) and GIN (content_tsv) indexes are created in the migration
and excluded from Alembic autogenerate comparison (see migrations/env.py).
"""

from __future__ import annotations

import uuid

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, Computed
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import EMBEDDING_DIM, KNOWLEDGE_SOURCE_TYPES, sql_in


class KnowledgeChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_chunks"

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
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    chunk_index: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Keyword/BM25-style search vector, generated from content (English config).
    content_tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content)", persisted=True)
    )
    # Half-precision embedding (~50% smaller index, negligible recall loss).
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBEDDING_DIM))
    # `metadata` is reserved on declarative classes — map the attribute as `meta`.
    meta: Mapped[dict | None] = mapped_column("metadata", JSONB)
    token_count: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint(
            sql_in("source_type", KNOWLEDGE_SOURCE_TYPES),
            name="ck_knowledge_chunks_source_type",
        ),
        Index("ix_knowledge_chunks_org_lead", "organization_id", "lead_id"),
        Index("ix_knowledge_chunks_source", "source_type", "source_id"),
        # Hybrid-search indexes — declared here so the model is the single source
        # of truth and `alembic check` watches them.
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
        Index(
            "ix_knowledge_chunks_content_tsv_gin",
            "content_tsv",
            postgresql_using="gin",
        ),
    )
