"""Two-tier case summary — attorney-facing case brief.

Revision ID: 0016_lead_case_brief
Revises: 0015_lead_profile_fields
Create Date: 2026-06-22

Adds leads.case_brief (JSONB): a richer, sectioned brief for attorney triage,
shown ONLY on the internal lead detail. The short, client-safe ai_summary is
unchanged (leads list + client portal). The brief is regenerated each call from
the full accumulated record, so it stays current and is cumulative for returning
callers without leaking internal assessment to the client view.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_lead_case_brief"
down_revision: str | None = "0015_lead_profile_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column("case_brief", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("leads", "case_brief")
