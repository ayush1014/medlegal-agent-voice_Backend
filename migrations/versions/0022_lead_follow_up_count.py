"""Per-phase follow-up attempt counter (anti-spam cap).

Revision ID: 0022_lead_follow_up_count
Revises: 0021_retainer_signer_name
Create Date: 2026-06-22

Counts automated reminders sent in the current funnel phase (doc collection or LOR
signing) so the dynamic follow-up loop can cap attempts and flag a human instead of
nudging forever. Reset to 0 when the lead advances a phase.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_lead_follow_up_count"
down_revision: str | None = "0021_retainer_signer_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("follow_up_count", sa.Integer(), nullable=False,
                                     server_default="0"))


def downgrade() -> None:
    op.drop_column("leads", "follow_up_count")
