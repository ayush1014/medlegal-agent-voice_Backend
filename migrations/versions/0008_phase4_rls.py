"""Phase 4 — grants, append-only enforcement, and RLS policies

Revision ID: 0008_phase4_rls
Revises: 0007_ai_memory_events
Create Date: 2026-06-20

AI-memory tables (RAG/KG/agent) are staff/system-only, scoped to an accessible
lead. `agent_events` and `outbox_events` are append-only for the app role
(`outbox_events` status transitions are made by the publisher via the owner
connection). `webhook_events` is infrastructure (no tenant) — no RLS; the system
ingestion path reads/writes it.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_phase4_rls"
down_revision: str | None = "0007_ai_memory_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Lead-scoped, staff/system, full CRUD.
_LEAD_CRUD = (
    "knowledge_chunks",
    "kg_nodes",
    "kg_edges",
    "agent_threads",
    "agent_checkpoints",
)
# Lead-scoped, append-only.
_LEAD_APPEND_ONLY = ("agent_events",)
# Tenant-scoped, append-only for the app role (publisher updates via owner).
_ORG_APPEND_ONLY = ("outbox_events",)
# Infrastructure (no tenant / no RLS).
_INFRA = ("webhook_events",)

_RLS_TABLES = (*_LEAD_CRUD, *_LEAD_APPEND_ONLY, *_ORG_APPEND_ONLY)


def upgrade() -> None:
    # --- Privileges (guarded so dev/owner-only still applies) ---
    crud_grants = "; ".join(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{t} TO app_user"
        for t in _LEAD_CRUD
    )
    append_grants = "; ".join(
        f"GRANT SELECT, INSERT ON public.{t} TO app_user; "
        f"REVOKE UPDATE, DELETE ON public.{t} FROM app_user"
        for t in (*_LEAD_APPEND_ONLY, *_ORG_APPEND_ONLY)
    )
    infra_grants = "; ".join(
        f"GRANT SELECT, INSERT, UPDATE ON public.{t} TO app_user" for t in _INFRA
    )
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
            {crud_grants};
            {append_grants};
            {infra_grants};
          END IF;
        END $$;
        """
    )

    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")

    # Lead-scoped tables (CRUD + append-only): staff/system, accessible lead.
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

    # outbox_events: firm-scoped; emitted in-tenant by staff/system.
    op.execute(
        """
        CREATE POLICY outbox_events_access ON public.outbox_events
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


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS outbox_events_access ON public.outbox_events")
    for table in (*_LEAD_CRUD, *_LEAD_APPEND_ONLY):
        op.execute(f"DROP POLICY IF EXISTS {table}_access ON public.{table}")
    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
