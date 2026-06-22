"""Document AI — classification + extraction fields on documents.

Revision ID: 0019_document_ai_fields
Revises: 0018_incident_state
Create Date: 2026-06-22

Incoming files (email attachments / portal uploads) are classified by content
(gpt-4o vision for images, text for PDFs), matched to the requirement they satisfy,
summarized, and mined for structured data that re-sharpens the estimate. These
columns hold that AI output; the existing documents.document_request_id links the
file to the requirement it fulfilled.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_document_ai_fields"
down_revision: str | None = "0018_incident_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("doc_category", sa.String(48), nullable=True))
    op.add_column("documents", sa.Column("classification_confidence", sa.Numeric(4, 3), nullable=True))
    op.add_column("documents", sa.Column("doc_summary", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("extracted", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    # processing | matched | needs_review | unmatched | failed
    op.add_column("documents", sa.Column("match_status", sa.String(16), nullable=True))


def downgrade() -> None:
    for col in ("match_status", "extracted", "doc_summary", "classification_confidence", "doc_category"):
        op.drop_column("documents", col)
