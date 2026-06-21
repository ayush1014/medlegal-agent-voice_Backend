"""Twilio voice webhooks: inbound call → LiveKit bridge, plus status/recording.

Inbound resolves the firm from the dialed number, records the call under that
firm's system context, and returns TwiML that bridges the caller into a LiveKit
SIP room (or, until SIP is provisioned, captures a voicemail so no lead is lost).
All webhooks are deduped via `webhook_events`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status as http_status
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Dial, VoiceResponse

from app.config import settings
from app.database import session_scope
from app.security.context import system_context
from app.services import sms_service, voice_service

router = APIRouter(prefix="/voice", tags=["voice"])

# Twilio call statuses that mean the call is over.
_TERMINAL = {"completed", "no-answer", "failed", "busy", "canceled"}


def _twiml(vr: VoiceResponse) -> Response:
    return Response(content=str(vr), media_type="application/xml")


async def _validate_twilio(request: Request, form: dict) -> None:
    if not settings.twilio_validate_webhooks:
        return
    if not settings.twilio_auth_token:
        raise HTTPException(http_status.HTTP_503_SERVICE_UNAVAILABLE, "Telephony not configured")
    validator = RequestValidator(settings.twilio_auth_token)
    # Reconstruct the exact URL Twilio signed (use the public base behind a proxy).
    url = (
        settings.public_base_url.rstrip("/") + request.url.path
        if settings.public_base_url
        else str(request.url)
    )
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validator.validate(url, form, signature):
        raise HTTPException(http_status.HTTP_403_FORBIDDEN, "Invalid Twilio signature")


def _sip_host() -> str | None:
    """Bare SIP host for LiveKit (tolerate a stray sip:/sips:/scheme + slashes)."""
    raw = (settings.livekit_sip_uri or "").strip()
    if not raw:
        return None
    raw = raw.removeprefix("sips://").removeprefix("sip://")
    raw = raw.removeprefix("sips:").removeprefix("sip:")
    return raw.strip("/").strip() or None


def _bridge_or_voicemail(dialed: str) -> VoiceResponse:
    vr = VoiceResponse()
    host = _sip_host()
    if host:
        # User part = the dialed number so LiveKit's inbound trunk matches it.
        dial = Dial()
        dial.sip(f"sip:{dialed}@{host};transport=tls")
        vr.append(dial)
    else:
        # SIP not provisioned yet → capture a callback so the lead isn't lost.
        vr.say("Thank you for calling. Please leave your name and number after the tone, "
               "and the team will call you right back.")
        action = (settings.public_base_url or "").rstrip("/") + "/api/voice/recording"
        vr.record(max_length=120, action=action or None, play_beep=True)
        vr.hangup()
    return vr


@router.post("/inbound")
async def inbound(request: Request) -> Response:
    form = dict(await request.form())
    await _validate_twilio(request, form)

    to = form.get("To")
    frm = form.get("From")
    sid = form.get("CallSid")
    if not (to and sid):
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "Missing call fields")

    async with session_scope(None) as db:
        org = await voice_service.resolve_org_by_dialed_number(db, to)

    if org is None:
        vr = VoiceResponse()
        vr.say("We're sorry, this number is not in service.")
        vr.hangup()
        return _twiml(vr)

    async with session_scope(system_context(org)) as db:
        if await voice_service.claim_webhook(db, "twilio", f"{sid}:inbound", "call.inbound"):
            call_id = await voice_service.get_voice_call_by_sid(db, sid)
            if call_id is None:
                call_id = await voice_service.create_voice_call(
                    db, org, direction="inbound", from_e164=frm, to_e164=to,
                    provider_sid=sid, status="ringing",
                )
            # No LiveKit bridge → voicemail capture; still create a lead so it's
            # never lost (callback SMS goes out when the recording lands).
            if not settings.livekit_sip_uri:
                fallback = await voice_service.create_fallback_lead(
                    db, org, frm, "Voicemail / callback — call not bridged to the agent."
                )
                await voice_service.link_voice_call_lead(db, call_id, fallback)

    return _twiml(_bridge_or_voicemail(dialed=to))


@router.post("/status")
async def call_status(request: Request) -> Response:
    form = dict(await request.form())
    await _validate_twilio(request, form)

    to = form.get("To")
    sid = form.get("CallSid")
    call_status = form.get("CallStatus")
    duration = form.get("CallDuration")
    recording = form.get("RecordingUrl")
    if not sid:
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "Missing CallSid")

    org = None
    if to:
        async with session_scope(None) as db:
            org = await voice_service.resolve_org_by_dialed_number(db, to)

    drop_lead = None
    caller = None
    if org is not None:
        async with session_scope(system_context(org)) as db:
            if await voice_service.claim_webhook(db, "twilio", f"{sid}:status:{call_status}", "call.status"):
                await voice_service.finalize_voice_call(
                    db, sid,
                    status=call_status,
                    duration_seconds=int(duration) if duration and duration.isdigit() else None,
                    recording_url=recording,
                )
                # Call ended while intake was still in progress → dropped call.
                if (call_status or "").lower() in _TERMINAL:
                    vc = await voice_service.get_voice_call_summary(db, sid)
                    if vc:
                        caller = vc.from_e164
                        drop_lead = await voice_service.find_in_progress_lead(db, vc.id)

    if drop_lead and caller:
        await sms_service.send_resume_sms(org, drop_lead, caller)

    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.post("/recording")
async def recording(request: Request) -> Response:
    """Voicemail recording ready → attach it and text the caller a callback note."""
    form = dict(await request.form())
    await _validate_twilio(request, form)

    to = form.get("To")
    sid = form.get("CallSid")
    caller = form.get("From")
    recording_url = form.get("RecordingUrl")

    org = None
    if to:
        async with session_scope(None) as db:
            org = await voice_service.resolve_org_by_dialed_number(db, to)

    lead_id = None
    if org is not None and sid:
        async with session_scope(system_context(org)) as db:
            if await voice_service.claim_webhook(db, "twilio", f"{sid}:recording", "call.recording"):
                if recording_url:
                    await voice_service.finalize_voice_call(
                        db, sid, status=None, duration_seconds=None, recording_url=recording_url
                    )
                vc = await voice_service.get_voice_call_summary(db, sid)
                if vc:
                    lead_id = vc.lead_id
                    caller = caller or vc.from_e164

    if lead_id and caller:
        await sms_service.send_callback_sms(org, lead_id, caller)

    vr = VoiceResponse()
    vr.hangup()
    return _twiml(vr)
