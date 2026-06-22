"""Case outcomes — capture actuals so the model can be calibrated (scoring-plan #5).

Revision ID: 0020_lead_outcome
Revises: 0019_document_ai_fields
Create Date: 2026-06-22

Records the real result of a case (settled amount + outcome) so calibration can compare
the PREDICTED settlement (settlement_estimates) against the ACTUAL — surfacing where the
estimate is biased, per case type and confidence band. Calibration is forward-looking:
these columns fill as cases close; the report is empty (not broken) until then.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_lead_outcome"
down_revision: str | None = "0019_document_ai_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # settled | dropped | lost | referred_out
    op.add_column("leads", sa.Column("outcome", sa.String(24), nullable=True))
    op.add_column("leads", sa.Column("actual_settlement", sa.Numeric(12, 2), nullable=True))
    op.add_column("leads", sa.Column("outcome_recorded_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    for col in ("outcome_recorded_at", "actual_settlement", "outcome"):
        op.drop_column("leads", col)
