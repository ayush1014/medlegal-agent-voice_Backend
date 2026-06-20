"""Phase 1 — Row-Level Security: helper functions + per-table policies

Revision ID: 0002_rls_policies
Revises: 0001_tenancy_identity
Create Date: 2026-06-20

Enables RLS on the tenancy/identity tables and adds fail-closed policies driven
by the per-request ``app.*`` GUCs. The owner role bypasses RLS (BYPASSRLS) and so
is unaffected; the least-privilege ``app_user`` role is fully constrained.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_rls_policies"
down_revision: str | None = "0001_tenancy_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RLS_TABLES = ("organizations", "users", "user_sessions", "phone_numbers")


def upgrade() -> None:
    # --- Helper schema + GUC accessors (fail-closed: unset GUC -> NULL) ---
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.current_org() RETURNS uuid
          LANGUAGE sql STABLE AS
        $$ SELECT NULLIF(current_setting('app.current_org', true), '')::uuid $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.current_subject_type() RETURNS text
          LANGUAGE sql STABLE AS
        $$ SELECT NULLIF(current_setting('app.current_subject_type', true), '') $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.current_subject_id() RETURNS uuid
          LANGUAGE sql STABLE AS
        $$ SELECT NULLIF(current_setting('app.current_subject_id', true), '')::uuid $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.current_role() RETURNS text
          LANGUAGE sql STABLE AS
        $$ SELECT NULLIF(current_setting('app.current_role', true), '') $$;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
            GRANT USAGE ON SCHEMA app TO app_user;
            GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app TO app_user;
          END IF;
        END $$;
        """
    )

    # --- Enable RLS on every tenant table ---
    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")

    # --- Policies (one permissive FOR ALL policy per table) ---

    # An organization row is visible only to subjects within that organization.
    op.execute(
        """
        CREATE POLICY org_isolation ON public.organizations
          FOR ALL
          USING (id = app.current_org())
          WITH CHECK (id = app.current_org())
        """
    )

    # Staff/system see firm users; clients never see staff.
    op.execute(
        """
        CREATE POLICY users_tenant_isolation ON public.users
          FOR ALL
          USING (
            organization_id = app.current_org()
            AND app.current_subject_type() IN ('user', 'system')
          )
          WITH CHECK (
            organization_id = app.current_org()
            AND app.current_subject_type() IN ('user', 'system')
          )
        """
    )

    # Phone numbers are firm-scoped.
    op.execute(
        """
        CREATE POLICY phone_numbers_tenant_isolation ON public.phone_numbers
          FOR ALL
          USING (organization_id = app.current_org())
          WITH CHECK (organization_id = app.current_org())
        """
    )

    # A subject can see and manage only its own sessions, within its firm.
    op.execute(
        """
        CREATE POLICY user_sessions_owner_only ON public.user_sessions
          FOR ALL
          USING (
            organization_id = app.current_org()
            AND subject_id = app.current_subject_id()
          )
          WITH CHECK (
            organization_id = app.current_org()
            AND subject_id = app.current_subject_id()
          )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS user_sessions_owner_only ON public.user_sessions")
    op.execute(
        "DROP POLICY IF EXISTS phone_numbers_tenant_isolation ON public.phone_numbers"
    )
    op.execute("DROP POLICY IF EXISTS users_tenant_isolation ON public.users")
    op.execute("DROP POLICY IF EXISTS org_isolation ON public.organizations")

    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")

    op.execute("DROP FUNCTION IF EXISTS app.current_role()")
    op.execute("DROP FUNCTION IF EXISTS app.current_subject_id()")
    op.execute("DROP FUNCTION IF EXISTS app.current_subject_type()")
    op.execute("DROP FUNCTION IF EXISTS app.current_org()")
    op.execute("DROP SCHEMA IF EXISTS app")
