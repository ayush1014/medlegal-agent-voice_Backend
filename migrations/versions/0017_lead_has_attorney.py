"""Structured representation flag (leads.has_attorney).

Revision ID: 0017_lead_has_attorney
Revises: 0016_lead_case_brief
Create Date: 2026-06-22

Persists whether the client already has an attorney as a column, so the funnel
reads the LATEST call's status authoritatively rather than a sticky substring in
ai_summary. A returning caller who fires their attorney must flip yes->no — the
old substring check matched a historical "yes" anywhere in the accumulated summary
and stayed stuck. has_attorney_flag now reads this column first.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_lead_has_attorney"
down_revision: str | None = "0016_lead_case_brief"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("has_attorney", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "has_attorney")
