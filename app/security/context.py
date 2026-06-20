"""Request-scoped tenant context.

The authenticated identity (which org, which subject, which role) is stored in a
``ContextVar`` and pushed into Postgres as ``app.*`` GUCs at the start of each DB
transaction so Row-Level Security can enforce isolation. Until the auth PRD lands,
the context is set explicitly (e.g. in tests); RLS is fail-closed, so an unset
context yields zero rows.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class TenantContext:
    organization_id: uuid.UUID
    subject_type: str  # "user" | "client" | "system"
    subject_id: uuid.UUID | None = None
    role: str | None = None


def system_context(organization_id: uuid.UUID) -> TenantContext:
    """The trusted service identity scoped to one firm. Used for pre-auth/bootstrap
    work (login lookups, signup, provisioning) — RLS still pins it to this org."""
    return TenantContext(
        organization_id=organization_id, subject_type="system", subject_id=None, role=None
    )


_current_context: ContextVar[TenantContext | None] = ContextVar(
    "tenant_context", default=None
)


def get_current_context() -> TenantContext | None:
    return _current_context.get()


def set_current_context(ctx: TenantContext | None) -> Token:
    return _current_context.set(ctx)


def reset_current_context(token: Token) -> None:
    _current_context.reset(token)
