"""Phase 1 — tenancy & identity tables

Revision ID: 0001_tenancy_identity
Revises:
Create Date: 2026-06-20
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.models.enums import SUBSCRIPTION_STATUSES, SUBJECT_TYPES, USER_ROLES, sql_in

revision: str = "0001_tenancy_identity"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables the least-privilege app role gets CRUD on. Granted here (in addition to
# the provision script's default privileges) so grants hold regardless of order.
_APP_TABLES = ("organizations", "users", "user_sessions", "phone_numbers")

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB
TS = sa.TIMESTAMP(timezone=True)


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id", UUID, primary_key=True, server_default=sa.text("uuidv7()")
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
    ]


def upgrade() -> None:
    op.create_table(
        "organizations",
        _uuid_pk(),
        *_timestamps(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(120), nullable=False, unique=True),
        sa.Column("intake_phone_e164", sa.String(20)),
        sa.Column("timezone", sa.String(64), nullable=False, server_default=sa.text("'UTC'")),
        sa.Column(
            "subscription_status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'trial'"),
        ),
        sa.Column("settings", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            sql_in("subscription_status", SUBSCRIPTION_STATUSES),
            name="ck_organizations_subscription_status",
        ),
    )

    op.create_table(
        "users",
        _uuid_pk(),
        *_timestamps(),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("full_name", sa.String(255)),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("last_login_at", TS),
        sa.Column("mfa_enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.UniqueConstraint("organization_id", "email", name="uq_users_org_email"),
        sa.CheckConstraint(sql_in("role", USER_ROLES), name="ck_users_role"),
    )
    op.create_index("ix_users_organization_id", "users", ["organization_id"])

    op.create_table(
        "user_sessions",
        _uuid_pk(),
        *_timestamps(),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_id", UUID, nullable=False),
        sa.Column("refresh_token_hash", sa.String(255), nullable=False),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("ip", postgresql.INET),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS),
        sa.CheckConstraint(
            sql_in("subject_type", SUBJECT_TYPES), name="ck_user_sessions_subject_type"
        ),
    )
    op.create_index(
        "ix_user_sessions_organization_id", "user_sessions", ["organization_id"]
    )
    op.create_index(
        "ix_user_sessions_subject", "user_sessions", ["subject_type", "subject_id"]
    )

    op.create_table(
        "phone_numbers",
        _uuid_pk(),
        *_timestamps(),
        sa.Column(
            "organization_id",
            UUID,
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("e164", sa.String(20), nullable=False, unique=True),
        sa.Column("provider", sa.String(32), nullable=False, server_default=sa.text("'twilio'")),
        sa.Column("provider_sid", sa.String(64)),
        sa.Column("capabilities", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "ix_phone_numbers_organization_id", "phone_numbers", ["organization_id"]
    )

    # Grant CRUD to the app role if it exists (provisioned separately). Guarded so
    # the migration also applies in dev where only the owner role is present.
    grants = "; ".join(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{t} TO app_user" for t in _APP_TABLES
    )
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
            {grants};
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_phone_numbers_organization_id", table_name="phone_numbers")
    op.drop_table("phone_numbers")
    op.drop_index("ix_user_sessions_subject", table_name="user_sessions")
    op.drop_index("ix_user_sessions_organization_id", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_index("ix_users_organization_id", table_name="users")
    op.drop_table("users")
    op.drop_table("organizations")
