"""Phase 2 — grants, append-only enforcement, and RLS policies

Revision ID: 0004_phase2_rls
Revises: 0003_core_pi_ai
Create Date: 2026-06-20

Lead visibility rules:
  - system (agent): all leads in its firm
  - staff user: owner/admin see all firm leads; others only their assigned leads
  - client: only the single lead their account is bound to

Child fact/AI tables follow the parent lead's visibility (staff/system only).
`settlement_estimates` and `lead_scores` are append-only (SELECT/INSERT, no
UPDATE/DELETE) — enforced by revoking privileges from the app role.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_phase2_rls"
down_revision: str | None = "0003_core_pi_ai"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables that follow lead visibility and allow normal CRUD by staff/system.
_LEAD_CHILD_TABLES = (
    "incidents",
    "injuries",
    "medical_treatments",
    "insurance_policies",
    "parties",
    "damages",
    "intake_transcripts",
    "transcript_segments",
)
# Append-only AI outputs (SELECT/INSERT only).
_APPEND_ONLY_TABLES = ("settlement_estimates", "lead_scores")

_ALL_RLS_TABLES = (
    "client_accounts",
    "leads",
    *_LEAD_CHILD_TABLES,
    *_APPEND_ONLY_TABLES,
)


def upgrade() -> None:
    # --- Privileges for the app role (guarded so dev/owner-only still applies) ---
    crud_tables = ("client_accounts", "leads", *_LEAD_CHILD_TABLES)
    crud_grants = "; ".join(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{t} TO app_user"
        for t in crud_tables
    )
    append_grants = "; ".join(
        f"GRANT SELECT, INSERT ON public.{t} TO app_user; "
        f"REVOKE UPDATE, DELETE ON public.{t} FROM app_user"
        for t in _APPEND_ONLY_TABLES
    )
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
            {crud_grants};
            {append_grants};
          END IF;
        END $$;
        """
    )

    # --- RLS helper functions (SECURITY DEFINER bypasses RLS internally, so the
    #     policies that call them can't recurse) ---
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.current_client_lead() RETURNS uuid
          LANGUAGE sql STABLE SECURITY DEFINER
          SET search_path = pg_catalog, public, app
        AS $$
          SELECT lead_id FROM public.client_accounts
          WHERE id = app.current_subject_id()
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.can_access_lead(p_lead uuid) RETURNS boolean
          LANGUAGE sql STABLE SECURITY DEFINER
          SET search_path = pg_catalog, public, app
        AS $$
          SELECT EXISTS (
            SELECT 1 FROM public.leads l
            WHERE l.id = p_lead
              AND l.organization_id = app.current_org()
              AND l.deleted_at IS NULL
              AND (
                app.current_subject_type() = 'system'
                -- Current scope: every firm user is owner/admin, so the
                -- owner/admin branch gives full firm view. The assigned_user_id
                -- branch is dormant (we mint no non-admin staff) — harmless
                -- superset, kept for when/if role tiers are introduced.
                OR (app.current_subject_type() = 'user'
                    AND (app.current_role() IN ('owner', 'admin')
                         OR l.assigned_user_id = app.current_subject_id()))
                OR (app.current_subject_type() = 'client'
                    AND l.id = app.current_client_lead())
              )
          )
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
            GRANT EXECUTE ON FUNCTION app.current_client_lead() TO app_user;
            GRANT EXECUTE ON FUNCTION app.can_access_lead(uuid) TO app_user;
          END IF;
        END $$;
        """
    )

    # --- Enable RLS on every Phase 2 table ---
    for table in _ALL_RLS_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")

    # client_accounts: staff/system see firm accounts; a client sees only its own.
    op.execute(
        """
        CREATE POLICY client_accounts_access ON public.client_accounts
          FOR ALL
          USING (
            organization_id = app.current_org()
            AND (
              app.current_subject_type() IN ('user', 'system')
              OR (app.current_subject_type() = 'client' AND id = app.current_subject_id())
            )
          )
          WITH CHECK (
            organization_id = app.current_org()
            AND app.current_subject_type() IN ('user', 'system')
          )
        """
    )

    # leads: read via the visibility helper (covers client-own-lead);
    # writes are staff/system only.
    op.execute(
        """
        CREATE POLICY leads_access ON public.leads
          FOR ALL
          USING (app.can_access_lead(id))
          WITH CHECK (
            organization_id = app.current_org()
            AND app.current_subject_type() IN ('user', 'system')
          )
        """
    )

    # Child fact + AI tables: staff/system only, scoped to an accessible lead.
    for table in (*_LEAD_CHILD_TABLES, *_APPEND_ONLY_TABLES):
        op.execute(
            f"""
            CREATE POLICY {table}_access ON public.{table}
              FOR ALL
              USING (
                organization_id = app.current_org()
                AND app.current_subject_type() IN ('user', 'system')
                AND app.can_access_lead(lead_id)
              )
              WITH CHECK (
                organization_id = app.current_org()
                AND app.current_subject_type() IN ('user', 'system')
                AND app.can_access_lead(lead_id)
              )
            """
        )


def downgrade() -> None:
    for table in (*_LEAD_CHILD_TABLES, *_APPEND_ONLY_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_access ON public.{table}")
    op.execute("DROP POLICY IF EXISTS leads_access ON public.leads")
    op.execute("DROP POLICY IF EXISTS client_accounts_access ON public.client_accounts")

    for table in _ALL_RLS_TABLES:
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")

    op.execute("DROP FUNCTION IF EXISTS app.can_access_lead(uuid)")
    op.execute("DROP FUNCTION IF EXISTS app.current_client_lead()")
