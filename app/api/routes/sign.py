"""Public Letter-of-Representation e-sign endpoints (the emailed magic-link flow).

No login: the single-purpose, time-limited token in the URL is the authorization. The
frontend `/sign/{code}` page renders the LOR (GET) and submits the typed signature (POST).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.services import retainer_service

router = APIRouter(prefix="/sign", tags=["sign"])


@router.get("/{code}")
async def view_lor(code: str) -> dict:
    try:
        return await retainer_service.lor_view(code)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None


class SignBody(BaseModel):
    signed_name: str


@router.post("/{code}")
async def sign_lor(code: str, body: SignBody, request: Request) -> dict:
    name = (body.signed_name or "").strip()
    if len(name) < 2:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Please type your full legal name to sign.")
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    try:
        return await retainer_service.sign_via_code(code, name, ip=ip, user_agent=ua)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None
