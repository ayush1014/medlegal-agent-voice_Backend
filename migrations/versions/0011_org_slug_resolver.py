"""PRD-1 — org-slug resolver for firm-branded login

Revision ID: 0011_org_slug_resolver
Revises: 0010_users_phone_per_org
Create Date: 2026-06-20

Firm-branded login resolves the organization BEFORE credentials, but at that
point there is no tenant context, so RLS on `organizations` would hide every
row. This SECURITY DEFINER function (owner-owned → bypasses RLS) lets the app
role map a public slug to an org id without opening a global read surface.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_org_slug_resolver"
down_revision: str | None = "0010_users_phone_per_org"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.org_id_for_slug(p_slug text) RETURNS uuid
          LANGUAGE sql STABLE SECURITY DEFINER
          SET search_path = pg_catalog, public, app
        AS $$
          SELECT id FROM public.organizations WHERE slug = p_slug
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
            GRANT EXECUTE ON FUNCTION app.org_id_for_slug(text) TO app_user;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app.org_id_for_slug(text)")
