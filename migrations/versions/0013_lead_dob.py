"""Returning-caller verification — leads.date_of_birth

Revision ID: 0013_lead_dob
Revises: 0012_org_phone_resolver
Create Date: 2026-06-21

A returning caller (same phone) is verified by name + DOB before the agent shares
any prior case context. We capture DOB at intake so it's available to verify on
the next call.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_lead_dob"
down_revision: str | None = "0012_org_phone_resolver"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("date_of_birth", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "date_of_birth")
