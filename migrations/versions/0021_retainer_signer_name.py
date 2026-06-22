"""Retainer signer name (typed e-signature).

Revision ID: 0021_retainer_signer_name
Revises: 0020_lead_outcome
Create Date: 2026-06-22

Stores the name the client typed when e-signing the Letter of Representation, recorded
alongside the existing signature_events (timestamp/IP/UA) for ESIGN/UETA validity.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_retainer_signer_name"
down_revision: str | None = "0020_lead_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("retainers", sa.Column("signer_name", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("retainers", "signer_name")
