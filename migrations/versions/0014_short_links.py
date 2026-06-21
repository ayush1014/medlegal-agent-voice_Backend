"""Short links for SMS/WhatsApp (clickable upload + sign URLs)

Revision ID: 0014_short_links
Revises: 0013_lead_dob
Create Date: 2026-06-21

A signed JWT in the URL is ~250 chars and SMS clients won't linkify it. We map a
short random code -> (org, lead, purpose) so the texted link is e.g. /u/Ab3xK9.
Accessed only via the owner connection (pre-auth resolution + funnel creation), so
no RLS policy / app_user grant is needed; codes are unguessable secrets.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID

revision: str = "0014_short_links"
down_revision: str | None = "0013_lead_dob"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "short_links",
        sa.Column("code", sa.String(16), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lead_id", UUID(as_uuid=True),
                  sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("short_links")
