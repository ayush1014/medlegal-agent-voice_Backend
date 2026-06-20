"""Request/response models for the auth endpoints."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field

from app.models.enums import CASE_TYPES


class OtpRequestIn(BaseModel):
    phone: str
    # Informational only; the server resolves the actual outcome on verify.
    purpose: str = "login"


class OtpVerifyIn(BaseModel):
    phone: str
    code: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class ClientSignupIn(BaseModel):
    signup_token: str
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    full_name: str = Field(min_length=1, max_length=255)
    case_type: str
    incident_description: str | None = None
    injury_area: str | None = None
    incident_location: str | None = None

    def valid_case_type(self) -> bool:
        return self.case_type in CASE_TYPES


class ProvisionIn(BaseModel):
    org_name: str = Field(min_length=1, max_length=255)
    org_slug: str = Field(min_length=1, max_length=120)
    intake_phone: str | None = None
    admin_email: EmailStr
    admin_phone: str | None = None
    admin_password: str = Field(min_length=8, max_length=256)
    role: str = "owner"


# --- Responses ---

class SessionOut(BaseModel):
    authenticated: bool = True
    subject_type: str
    role: str | None = None
    # Echoed so a split-origin SPA can send it as the X-CSRF-Token header.
    csrf_token: str | None = None


class OtpVerifyOut(BaseModel):
    """Either a session was established, or the phone is verified but has no
    account yet (the client should proceed to signup with `signup_token`)."""
    authenticated: bool
    subject_type: str | None = None
    role: str | None = None
    signup_token: str | None = None
    csrf_token: str | None = None


class MeOut(BaseModel):
    organization_id: str
    subject_type: str
    subject_id: str
    role: str | None = None


class MessageOut(BaseModel):
    status: str = "ok"
    csrf_token: str | None = None


class ProvisionOut(BaseModel):
    organization_id: str
    user_id: str
