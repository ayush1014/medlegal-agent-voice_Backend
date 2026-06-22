"""Incident jurisdiction — incidents.incident_state (2-letter US code).

Revision ID: 0018_incident_state
Revises: 0017_lead_has_attorney
Create Date: 2026-06-22

The state where the incident happened sets the legal jurisdiction (statute of
limitations + comparative-fault regime). Captured on the call (#1) and used by the
state-aware SOL signal + comparative-fault rules (see app/services/jurisdiction.py).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_incident_state"
down_revision: str | None = "0017_lead_has_attorney"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("incident_state", sa.String(2), nullable=True))


def downgrade() -> None:
    op.drop_column("incidents", "incident_state")
