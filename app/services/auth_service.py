"""Pre-auth identity operations.

Every function here runs under a *system* tenant context scoped to the resolved
org (set by the route via session_scope), so RLS still enforces firm isolation —
the org is fixed and cannot be crossed. No owner connection, no bypass.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def normalize_email(raw: str) -> str:
    return raw.strip().lower()


def normalize_phone(raw: str) -> str:
    """Best-effort E.164 (US default). Swap for libphonenumber when we go global."""
    if raw.strip().startswith("+"):
        digits = "+" + re.sub(r"\D", "", raw)
    else:
        d = re.sub(r"\D", "", raw)
        if len(d) == 10:
            digits = "+1" + d
        elif len(d) == 11 and d.startswith("1"):
            digits = "+" + d
        else:
            digits = "+" + d
    return digits


# --- Lookups (run under system context → RLS-scoped to the org) ----------

async def find_admin_by_email(db: AsyncSession, org: uuid.UUID, email: str):
    return (
        await db.execute(
            text(
                "SELECT id, password_hash, role, is_active FROM users "
                "WHERE organization_id = :org AND email = :email"
            ),
            {"org": org, "email": email},
        )
    ).first()


async def find_admin_by_phone(db: AsyncSession, org: uuid.UUID, phone: str):
    return (
        await db.execute(
            text(
                "SELECT id, role, is_active FROM users "
                "WHERE organization_id = :org AND phone = :phone"
            ),
            {"org": org, "phone": phone},
        )
    ).first()


async def find_client_by_email(db: AsyncSession, org: uuid.UUID, email: str):
    return (
        await db.execute(
            text(
                "SELECT id, password_hash, is_active, lead_id FROM client_accounts "
                "WHERE organization_id = :org AND email = :email"
            ),
            {"org": org, "email": email},
        )
    ).first()


async def find_client_by_phone(db: AsyncSession, org: uuid.UUID, phone: str):
    return (
        await db.execute(
            text(
                "SELECT id, is_active, lead_id FROM client_accounts "
                "WHERE organization_id = :org AND phone = :phone"
            ),
            {"org": org, "phone": phone},
        )
    ).first()


async def find_claimable_lead_by_phone(db: AsyncSession, org: uuid.UUID, phone: str):
    """A lead with this phone that has no client account yet (call-created lead)."""
    return (
        await db.execute(
            text(
                "SELECT l.id FROM leads l "
                "WHERE l.organization_id = :org AND l.phone = :phone "
                "AND l.deleted_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM client_accounts ca WHERE ca.lead_id = l.id) "
                "ORDER BY l.created_at DESC LIMIT 1"
            ),
            {"org": org, "phone": phone},
        )
    ).scalar_one_or_none()


# --- Mutations -----------------------------------------------------------

async def touch_last_login(db: AsyncSession, table: str, subject_id: uuid.UUID) -> None:
    # `table` is a fixed internal literal ('users'|'client_accounts'), never user input.
    await db.execute(
        text(f"UPDATE {table} SET last_login_at = now() WHERE id = :id"),
        {"id": subject_id},
    )


async def record_audit(
    db: AsyncSession,
    org: uuid.UUID,
    *,
    actor_type: str,
    actor_id: uuid.UUID | None,
    action: str,
) -> None:
    await db.execute(
        text(
            "INSERT INTO audit_logs (organization_id, actor_type, actor_id, action) "
            "VALUES (:org, :atype, :aid, :action)"
        ),
        {"org": org, "atype": actor_type, "aid": actor_id, "action": action},
    )


async def create_client_signup(
    db: AsyncSession,
    org: uuid.UUID,
    *,
    full_name: str,
    email: str,
    phone: str,
    password_hash: str,
    case_type: str,
    description: str | None,
    location: str | None,
    injury_area: str | None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create the draft lead (+ incident/injury seed) and the bound client account."""
    lead_id = uuid.uuid4()
    account_id = uuid.uuid4()

    await db.execute(
        text(
            "INSERT INTO leads (id, organization_id, full_name, phone, email, "
            "case_type, source, pipeline_status) "
            "VALUES (:id, :org, :name, :phone, :email, :ct, 'web', 'Intake Started')"
        ),
        {"id": lead_id, "org": org, "name": full_name, "phone": phone, "email": email, "ct": case_type},
    )
    if description or location:
        await db.execute(
            text(
                "INSERT INTO incidents (organization_id, lead_id, description, location_text) "
                "VALUES (:org, :lead, :desc, :loc)"
            ),
            {"org": org, "lead": lead_id, "desc": description, "loc": location},
        )
    if injury_area:
        await db.execute(
            text(
                "INSERT INTO injuries (organization_id, lead_id, body_part) "
                "VALUES (:org, :lead, :area)"
            ),
            {"org": org, "lead": lead_id, "area": injury_area},
        )
    await db.execute(
        text(
            "INSERT INTO client_accounts (id, organization_id, lead_id, email, phone, password_hash) "
            "VALUES (:id, :org, :lead, :email, :phone, :ph)"
        ),
        {"id": account_id, "org": org, "lead": lead_id, "email": email, "phone": phone, "ph": password_hash},
    )
    return account_id, lead_id


async def create_client_account_for_lead(
    db: AsyncSession,
    org: uuid.UUID,
    lead_id: uuid.UUID,
    *,
    phone: str,
    email: str | None = None,
    password_hash: str | None = None,
) -> uuid.UUID:
    """Claim an existing (call-created) lead by binding a new client account to it."""
    account_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO client_accounts (id, organization_id, lead_id, email, phone, password_hash) "
            "VALUES (:id, :org, :lead, :email, :phone, :ph)"
        ),
        {"id": account_id, "org": org, "lead": lead_id, "email": email, "phone": phone, "ph": password_hash},
    )
    return account_id


async def provision_org_and_admin(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    slug: str,
    intake_phone: str | None,
    email: str,
    phone: str | None,
    password_hash: str,
    role: str,
) -> uuid.UUID:
    """Bootstrap a firm + its first admin. Runs under system context with the
    new org id, so the organizations INSERT satisfies RLS (id = current_org)."""
    user_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO organizations (id, name, slug, intake_phone_e164) "
            "VALUES (:id, :name, :slug, :intake)"
        ),
        {"id": org_id, "name": name, "slug": slug, "intake": intake_phone},
    )
    if intake_phone:
        await db.execute(
            text(
                "INSERT INTO phone_numbers (organization_id, e164, is_primary) "
                "VALUES (:org, :e164, true)"
            ),
            {"org": org_id, "e164": intake_phone},
        )
    await db.execute(
        text(
            "INSERT INTO users (id, organization_id, email, phone, password_hash, role) "
            "VALUES (:id, :org, :email, :phone, :ph, :role)"
        ),
        {"id": user_id, "org": org_id, "email": email, "phone": phone, "ph": password_hash, "role": role},
    )
    return user_id
