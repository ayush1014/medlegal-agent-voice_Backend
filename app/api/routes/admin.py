"""Internal admin provisioning — NOT a public endpoint.

Gated by a shared secret header; disabled (404) when no secret is configured.
Creates a firm + its first admin. Enforces the role invariant (owner/admin only).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, status
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import session_scope
from app.schemas.auth import ProvisionIn, ProvisionOut
from app.security.context import system_context
from app.security.passwords import hash_password
from app.services import auth_service

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/provision", response_model=ProvisionOut)
async def provision(
    body: ProvisionIn, x_provision_secret: str | None = Header(default=None)
) -> ProvisionOut:
    # Hide the endpoint entirely unless the internal secret is set and matches.
    if not settings.provision_secret or x_provision_secret != settings.provision_secret:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    if body.role not in ("owner", "admin"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid role")

    org_id = uuid.uuid4()
    email = auth_service.normalize_email(body.admin_email)
    phone = auth_service.normalize_phone(body.admin_phone) if body.admin_phone else None
    intake = auth_service.normalize_phone(body.intake_phone) if body.intake_phone else None
    password_hash = hash_password(body.admin_password)

    try:
        async with session_scope(system_context(org_id)) as db:
            user_id = await auth_service.provision_org_and_admin(
                db, org_id=org_id, name=body.org_name, slug=body.org_slug,
                intake_phone=intake, email=email, phone=phone,
                password_hash=password_hash, role=body.role,
            )
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Slug or identifier already in use")

    return ProvisionOut(organization_id=str(org_id), user_id=str(user_id))
