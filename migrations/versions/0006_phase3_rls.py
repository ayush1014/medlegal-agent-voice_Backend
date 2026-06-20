"""Phase 3 — grants, append-only enforcement, and RLS policies

Revision ID: 0006_phase3_rls
Revises: 0005_comms_docs_workflow
Create Date: 2026-06-20

All Phase 3 tables are staff/system-only and scoped to an accessible lead (client
portal read/write policies are deferred to the client-portal PRD). `audit_logs`
is owner/admin/system readable. `signature_events` and `audit_logs` are
append-only (SELECT/INSERT; UPDATE/DELETE revoked from the app role).
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_phase3_rls"
down_revision: str | None = "0005_comms_docs_workflow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Lead-scoped, staff/system, full CRUD, NOT NULL lead_id.
_LEAD_CRUD = (
    "conversations",
    "messages",
    "document_requests",
    "documents",
    "retainers",
    "internal_notes",
)
# Lead-scoped but lead_id is nullable (call/task may precede a lead).
_LEAD_NULLABLE_CRUD = ("voice_calls", "tasks")
# Append-only, NOT NULL lead_id.
_LEAD_APPEND_ONLY = ("signature_events",)
# Append-only, org-scoped (no lead).
_ORG_APPEND_ONLY = ("audit_logs",)

_ALL = (*_LEAD_CRUD, *_LEAD_NULLABLE_CRUD, *_LEAD_APPEND_ONLY, *_ORG_APPEND_ONLY)


def upgrade() -> None:
    # --- Privileges (guarded so dev/owner-only still applies) ---
    crud = (*_LEAD_CRUD, *_LEAD_NULLABLE_CRUD)
    crud_grants = "; ".join(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{t} TO app_user" for t in crud
    )
    append_grants = "; ".join(
        f"GRANT SELECT, INSERT ON public.{t} TO app_user; "
        f"REVOKE UPDATE, DELETE ON public.{t} FROM app_user"
        for t in (*_LEAD_APPEND_ONLY, *_ORG_APPEND_ONLY)
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

    for table in _ALL:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")

    # NOT NULL lead tables (CRUD + append-only): staff/system, accessible lead.
    for table in (*_LEAD_CRUD, *_LEAD_APPEND_ONLY):
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

    # Nullable-lead tables: allow firm-wide rows with no lead, else accessible lead.
    for table in _LEAD_NULLABLE_CRUD:
        op.execute(
            f"""
            CREATE POLICY {table}_access ON public.{table}
              FOR ALL
              USING (
                organization_id = app.current_org()
                AND app.current_subject_type() IN ('user', 'system')
                AND (lead_id IS NULL OR app.can_access_lead(lead_id))
              )
              WITH CHECK (
                organization_id = app.current_org()
                AND app.current_subject_type() IN ('user', 'system')
                AND (lead_id IS NULL OR app.can_access_lead(lead_id))
              )
            """
        )

    # audit_logs: readable by owner/admin/system; appendable by staff/system.
    op.execute(
        """
        CREATE POLICY audit_logs_access ON public.audit_logs
          FOR ALL
          USING (
            organization_id = app.current_org()
            AND (
              app.current_subject_type() = 'system'
              OR (app.current_subject_type() = 'user'
                  AND app.current_role() IN ('owner', 'admin'))
            )
          )
          WITH CHECK (
            organization_id = app.current_org()
            AND app.current_subject_type() IN ('user', 'system')
          )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS audit_logs_access ON public.audit_logs")
    for table in (*_LEAD_CRUD, *_LEAD_APPEND_ONLY, *_LEAD_NULLABLE_CRUD):
        op.execute(f"DROP POLICY IF EXISTS {table}_access ON public.{table}")
    for table in _ALL:
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
