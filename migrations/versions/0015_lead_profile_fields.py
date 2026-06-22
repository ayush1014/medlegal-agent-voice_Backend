"""Lead profile enrichment — employment, income, address (stronger PI intake).

Revision ID: 0015_lead_profile_fields
Revises: 0014_short_links
Create Date: 2026-06-21

Captures more of the client profile on the call so the funnel (scoring,
qualification, settlement — esp. lost earnings) has richer inputs. email +
date_of_birth columns already exist (0013); this adds employment + location.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_lead_profile_fields"
down_revision: str | None = "0014_short_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("occupation", sa.String(255), nullable=True))
    op.add_column("leads", sa.Column("employer", sa.String(255), nullable=True))
    op.add_column("leads", sa.Column("employment_status", sa.String(64), nullable=True))
    op.add_column("leads", sa.Column("annual_income", sa.Numeric(12, 2), nullable=True))
    op.add_column("leads", sa.Column("address", sa.String(512), nullable=True))


def downgrade() -> None:
    for col in ("address", "annual_income", "employment_status", "employer", "occupation"):
        op.drop_column("leads", col)
