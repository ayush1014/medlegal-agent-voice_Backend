"""LiveKit voice agent worker (PRD-2 — realtime streaming pipeline).

Each inbound SIP call runs livekit-agents' native pipeline:
    Deepgram Nova 3 (STT) → DeepSeek (streaming LLM) → Deepgram Aura 2 (TTS)
with Silero VAD for barge-in. Using the native pipeline (instead of a custom
non-streaming adapter) means the agent:
  - streams tokens straight into TTS, so it starts speaking almost immediately;
  - stops and listens the moment the caller talks over it (barge-in), then answers;
  - keeps full conversation context for thoughtful, non-robotic replies.

Transcript persistence happens on `conversation_item_added` as fire-and-forget
tasks, so DB round-trips never sit on the reply's critical path. Realtime tools
are trimmed to the essentials (emergency + end) — structured facts are extracted
post-call (Phase E), so there's no per-turn tool round-trip slowing things down.

Org + caller are resolved from the SIP participant's dialed number.

Run:
    python -m app.agent.worker dev      # local
    python -m app.agent.worker start    # production
"""

from __future__ import annotations

import asyncio
import os

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import deepgram, openai, silero

from app.agent.context import IntakeContext
from app.agent.prompt import GREETING, render_system_prompt
from app.config import settings
from app.database import session_scope
from app.security.context import system_context
from app.services import intake_service, voice_service

# Deepgram Aura 2 voices per language (default personas — see PRD §13.1).
_AURA_VOICE = {"en": "aura-2-thalia-en", "es": "aura-2-celeste-es"}


def _returning_instructions(dossier: str) -> str:
    """Appended to the agent prompt for a recognized returning caller."""
    return (
        "\n\nRETURNING CALLER — this phone number is already in our system. Here is what we already know "
        "about this caller; do NOT ask for any of it again — acknowledge it and build on it:\n"
        f"{dossier}\n"
        "Warmly greet them by name, briefly confirm you're speaking with the right person (for privacy), "
        "then continue their existing case — ask only what's new, unclear, or still missing. Never make "
        "them repeat their story from scratch."
    )


def _greeting_for(ctx: IntakeContext) -> str:
    """Compliance greeting (recording + AI disclosure), personalized for returners."""
    if ctx.returning and ctx.known_name:
        first = ctx.known_name.split()[0]
        return (
            f"Thanks for calling {ctx.firm_name} — good to have you back, {first}. This call is recorded "
            "and you're speaking with an AI assistant. Para espanol, diga espanol."
        )
    return GREETING["en"].format(firm=ctx.firm_name)

# Named agent so the LiveKit SIP dispatch rule can auto-dispatch it on inbound calls.
AGENT_NAME = "medlegal-intake"


def _bootstrap_env() -> None:
    """The LiveKit/Deepgram SDKs read credentials from os.environ; mirror them
    from our validated settings so a single .env drives everything."""
    for name, value in {
        "LIVEKIT_URL": settings.livekit_url,
        "LIVEKIT_API_KEY": settings.livekit_api_key,
        "LIVEKIT_API_SECRET": settings.livekit_api_secret,
        "DEEPGRAM_API_KEY": settings.deepgram_api_key,
    }.items():
        if value and not os.environ.get(name):
            os.environ[name] = value


async def _resolve_call(ctx: JobContext) -> IntakeContext | None:
    """Resolve org + caller from the SIP participant's attributes."""
    participant = await ctx.wait_for_participant()
    attrs = participant.attributes or {}
    dialed = attrs.get("sip.trunkPhoneNumber") or attrs.get("sip.toNumber")
    caller = attrs.get("sip.phoneNumber") or attrs.get("sip.fromNumber") or "unknown"
    call_sid = attrs.get("sip.callID") or attrs.get("sip.twilio.callSid")

    if not dialed:
        return None
    async with session_scope(None) as db:
        org = await voice_service.resolve_org_by_dialed_number(db, dialed)
    if org is None:
        return None

    voice_call_id = None
    if call_sid:
        async with session_scope(None) as db:
            voice_call_id = await voice_service.get_voice_call_by_sid(db, call_sid)

    return IntakeContext(
        organization_id=org,
        caller_phone=caller,
        voice_call_id=voice_call_id,
        firm_name="medLegal",
    )


async def _persist_segment(ctx: IntakeContext, speaker: str, text: str) -> None:
    """Off-critical-path transcript write (best-effort)."""
    try:
        async with session_scope(system_context(ctx.organization_id)) as db:
            await intake_service.add_segment(db, ctx, speaker, text)
    except Exception:  # noqa: BLE001 - persistence must never break the call
        pass


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    intake_ctx = await _resolve_call(ctx)
    if intake_ctx is None:
        await ctx.shutdown(reason="org-unresolved")
        return

    # Resolve-or-create the lead by phone (returning callers keep one profile) +
    # this call's transcript. Load their dossier so the agent continues, not restarts.
    dossier = None
    async with session_scope(system_context(intake_ctx.organization_id)) as db:
        await intake_service.create_session_records(db, intake_ctx)
        if intake_ctx.returning:
            dossier = await intake_service.build_dossier(db, intake_ctx.lead_id)

    state = {"ended": False, "emergency": False}
    transcript_lines: list[str] = []

    # --- Trimmed realtime tools (no per-turn DB round-trip) ---
    @function_tool
    async def flag_emergency() -> str:
        """Flag a life-threatening medical emergency the caller describes."""
        state["emergency"] = True
        state["ended"] = True
        intake_ctx.emergency = True
        return "Tell the caller to hang up and dial 911 now; the firm will follow up by text."

    @function_tool
    async def end_intake(reason: str = "complete") -> str:
        """End the call after a brief goodbye — when intake is complete, the caller
        wants to stop, it's a wrong number, or clearly not an injury matter."""
        state["ended"] = True
        intake_ctx.end_reason = reason
        return "Acknowledged — give a short, warm goodbye."

    session = AgentSession(
        # no_delay + tighter endpointing = faster end-of-speech → lower turn latency.
        stt=deepgram.STT(
            model="nova-3", language="multi", interim_results=True, no_delay=True,
            endpointing_ms=300, api_key=settings.deepgram_api_key,
        ),
        # Native streaming LLM (DeepSeek, OpenAI-compatible) → tokens flow into TTS.
        llm=openai.LLM(
            model=settings.deepseek_realtime_model,
            base_url=settings.deepseek_base_url,
            api_key=settings.deepseek_api_key,
            temperature=0.4,
        ),
        tts=deepgram.TTS(model=_AURA_VOICE["en"], api_key=settings.deepgram_api_key),
        vad=silero.VAD.load(),
        # Lower = snappier; raise toward 0.6 if it cuts callers off mid-sentence.
        min_endpointing_delay=0.4,
        allow_interruptions=True,  # barge-in: stop & listen when the caller speaks
    )

    # --- Persist each finalized turn off the critical path ---
    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        msg = ev.item
        text = (getattr(msg, "text_content", None) or "").strip()
        role = getattr(msg, "role", None)
        if not text or role not in ("user", "assistant"):
            return
        speaker = "caller" if role == "user" else "agent"
        transcript_lines.append(f"{'Caller' if speaker == 'caller' else 'Agent'}: {text}")
        asyncio.create_task(_persist_segment(intake_ctx, speaker, text))

    async def _finalize() -> None:
        status = "complete" if state["ended"] else "failed"
        transcript_text = "\n".join(transcript_lines)
        try:
            async with session_scope(system_context(intake_ctx.organization_id)) as db:
                await intake_service.finalize_transcript(
                    db, intake_ctx, status=status, full_text=transcript_text
                )
        except Exception:  # noqa: BLE001
            pass
        # Post-call handoff: extraction → memory → intake.completed → welcome SMS.
        try:
            from app.services.intake_pipeline import run_post_call_pipeline

            await run_post_call_pipeline(
                organization_id=intake_ctx.organization_id,
                lead_id=intake_ctx.lead_id,
                transcript_text=transcript_text,
                transcript_id=intake_ctx.transcript_id,
                voice_call_id=intake_ctx.voice_call_id,
                caller_phone=intake_ctx.caller_phone,
            )
        except Exception:  # noqa: BLE001 - never crash teardown; a publisher can retry
            pass

    ctx.add_shutdown_callback(_finalize)

    instructions = render_system_prompt(intake_ctx.firm_name, "en")
    if dossier:
        instructions += _returning_instructions(dossier)
    agent = Agent(instructions=instructions, tools=[flag_emergency, end_intake])
    await session.start(agent=agent, room=ctx.room)
    # Scripted recording/AI disclosure (compliance), personalized for returning callers.
    await session.say(_greeting_for(intake_ctx))


def main() -> None:
    _bootstrap_env()
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME))


if __name__ == "__main__":
    main()
