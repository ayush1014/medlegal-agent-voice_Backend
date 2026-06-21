"""PRD-2 — dialed-number → org resolver for inbound calls

Revision ID: 0012_org_phone_resolver
Revises: 0011_org_slug_resolver
Create Date: 2026-06-20

An inbound call resolves its firm from the dialed number before any tenant
context exists, so (like the slug resolver) this SECURITY DEFINER function maps
a provisioned Twilio number to its organization without opening a global read
surface on phone_numbers.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_org_phone_resolver"
down_revision: str | None = "0011_org_slug_resolver"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.org_id_for_phone(p_e164 text) RETURNS uuid
          LANGUAGE sql STABLE SECURITY DEFINER
          SET search_path = pg_catalog, public, app
        AS $$
          SELECT organization_id FROM public.phone_numbers WHERE e164 = p_e164
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
            GRANT EXECUTE ON FUNCTION app.org_id_for_phone(text) TO app_user;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app.org_id_for_phone(text)")
