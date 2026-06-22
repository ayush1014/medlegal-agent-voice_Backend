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
import logging
import os
import re
from datetime import date as _date

from sqlalchemy import text

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomInputOptions,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    function_tool,
    metrics,
)
from livekit.agents.voice.turn import EndpointingOptions, InterruptionOptions
from livekit.plugins import deepgram, noise_cancellation, openai, silero

logger = logging.getLogger("medlegal.voice")

from app.agent.context import IntakeContext
from app.agent.prompt import GREETING, render_system_prompt
from app.config import settings
from app.database import session_scope
from app.security.context import system_context
from app.services import context_service, intake_service, outbox_service, voice_service
from app.services.context_service import ContextPack

# Deepgram Aura 2 voice (English-only for v1).
_TTS_VOICE = "aura-2-asteria-en"


def _names_match(spoken: str, stored: str | None) -> bool:
    """Loose name match for identity verification — one name's tokens a subset of
    the other's (handles middle names, ordering, punctuation)."""
    def toks(s: str | None) -> set[str]:
        return {t for t in re.sub(r"[^a-z ]", " ", (s or "").lower()).split() if t}
    a, b = toks(spoken), toks(stored)
    return bool(a and b and (a <= b or b <= a))


def _greeting_for(ctx: IntakeContext, pack: ContextPack) -> str:
    """Compliance greeting (recording + AI disclosure). Personalized only when the
    context pack actually warrants it (known name + real recalled context)."""
    if pack.warm_ok() and pack.anchor and pack.anchor.full_name:
        first = pack.anchor.full_name.split()[0]
        return (
            f"Thanks for calling {ctx.firm_name} — good to have you back, {first}. This call is recorded "
            "and you're speaking with an AI assistant. How can I help you today?"
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
    # this call's transcript, then assemble the Hybrid RAG + KG context pack so the
    # agent continues the case instead of restarting. Recap mode = no network.
    async with session_scope(system_context(intake_ctx.organization_id)) as db:
        await intake_service.create_session_records(db, intake_ctx)
        pack = await context_service.assemble_context(
            intake_ctx.organization_id, intake_ctx.lead_id,
            returning=intake_ctx.returning, current_transcript_id=intake_ctx.transcript_id, db=db,
        )

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

    @function_tool
    async def verify_caller(full_name: str, date_of_birth: str) -> str:
        """Verify a RETURNING caller's identity against the record BEFORE discussing
        their existing case. Pass the caller's stated full name and date of birth
        normalized as YYYY-MM-DD. Returns whether they match the file."""
        try:
            async with session_scope(system_context(intake_ctx.organization_id)) as db:
                row = (await db.execute(
                    text("SELECT full_name, date_of_birth FROM leads WHERE id = :l"),
                    {"l": intake_ctx.lead_id})).first()
        except Exception:  # noqa: BLE001 - never break the call on a lookup hiccup
            return "Couldn't check the record right now — continue and collect their details."
        if row is None or row.date_of_birth is None:
            return "No identity on file to verify — proceed with intake as usual."
        try:
            spoken = _date.fromisoformat(date_of_birth.strip())
        except (ValueError, AttributeError):
            return "That date wasn't clear — ask them to repeat their date of birth."
        if _names_match(full_name, row.full_name) and spoken == row.date_of_birth:
            intake_ctx.verified = True
            return "VERIFIED — identity confirmed. You may continue with their case."
        return ("NOT a match. Ask once more; if it still doesn't match, warmly acknowledge "
                "you couldn't verify and continue WITHOUT sharing any prior case details.")

    session = AgentSession(
        # no_delay + tighter endpointing = faster end-of-speech → lower turn latency.
        stt=deepgram.STT(
            model="nova-3", language="en", interim_results=True, no_delay=True,
            endpointing_ms=300, api_key=settings.deepgram_api_key,
        ),
        # Native streaming LLM (OpenAI gpt-4o-mini) → tokens flow straight into TTS.
        llm=openai.LLM(
            model=settings.voice_llm_model,
            api_key=settings.openai_api_key,
            temperature=0.4,
        ),
        tts=deepgram.TTS(model=_TTS_VOICE, api_key=settings.deepgram_api_key),
        vad=silero.VAD.load(),
        # min_delay = snappy floor when the turn detector is confident the caller
        # finished; max_delay caps the wait when it's UNSURE (was 2.5s → dead air,
        # and mis-fired even on complete short answers like "My name is Ayush").
        # Raise max_delay toward 2.5 if it starts cutting callers off mid-thought.
        #
        # Barge-in ON (natural conversation). The reason it used to cut the agent off
        # mid-spelling was telephony ECHO — the agent heard its own TTS and thought the
        # caller was interrupting. That's now killed by BVCTelephony noise cancellation
        # on the room input (see session.start below), so interruptions only fire on
        # the REAL caller. min_delay/max_delay bound the end-of-turn wait.
        turn_handling=TurnHandlingOptions(
            endpointing=EndpointingOptions(min_delay=0.4, max_delay=1.5),
            interruption=InterruptionOptions(enabled=True),
        ),
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

    # --- Per-stage latency instrumentation (the numbers that matter for tuning) ---
    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:
        m = ev.metrics
        if isinstance(m, metrics.LLMMetrics):
            logger.info("⏱ LLM ttft=%.2fs total=%.2fs tok/s=%.1f prompt_tok=%s",
                        getattr(m, "ttft", -1), getattr(m, "duration", -1),
                        getattr(m, "tokens_per_second", -1), getattr(m, "prompt_tokens", "?"))
        elif isinstance(m, metrics.TTSMetrics):
            logger.info("⏱ TTS ttfb=%.2fs total=%.2fs", getattr(m, "ttfb", -1), getattr(m, "duration", -1))
        elif isinstance(m, metrics.STTMetrics):
            logger.info("⏱ STT audio=%.2fs duration=%.2fs", getattr(m, "audio_duration", -1),
                        getattr(m, "duration", -1))
        elif isinstance(m, metrics.EOUMetrics):
            logger.info("⏱ EOU end_of_utterance=%.2fs transcription_delay=%.2fs",
                        getattr(m, "end_of_utterance_delay", -1), getattr(m, "transcription_delay", -1))

    async def _finalize() -> None:
        # Shutdown runs against a hard kill timer — do ONLY fast DB work here:
        # persist the transcript and emit `call.ended`. The heavy pipeline
        # (extraction → memory → intelligence) is drained server-side by the
        # post-call worker, so a hangup can never kill it mid-flight.
        status = "complete" if state["ended"] else "failed"
        transcript_text = "\n".join(transcript_lines)
        try:
            async with session_scope(system_context(intake_ctx.organization_id)) as db:
                await intake_service.finalize_transcript(
                    db, intake_ctx, status=status, full_text=transcript_text
                )
                await outbox_service.emit_event(
                    db, intake_ctx.organization_id,
                    aggregate_type="lead", aggregate_id=intake_ctx.lead_id,
                    event_type="call.ended",
                    payload={
                        "transcript_id": str(intake_ctx.transcript_id) if intake_ctx.transcript_id else None,
                        "voice_call_id": str(intake_ctx.voice_call_id) if intake_ctx.voice_call_id else None,
                        "caller_phone": intake_ctx.caller_phone,
                    },
                )
        except Exception:  # noqa: BLE001 - teardown must never raise
            logger.exception("finalize/call.ended emit failed for lead %s", intake_ctx.lead_id)

    ctx.add_shutdown_callback(_finalize)

    instructions = render_system_prompt(intake_ctx.firm_name, "en")
    block = pack.to_prompt()  # Hybrid RAG + KG memory briefing (or "" for a brand-new caller)
    if block:
        instructions += "\n\n" + block
    # verify_caller only for returning callers (new callers have nothing to verify).
    tools = [flag_emergency, end_intake]
    if intake_ctx.returning:
        tools.append(verify_caller)
    agent = Agent(instructions=instructions, tools=tools)
    # Prompt size drives prefill latency on every turn — log it once so we can see the cost.
    logger.info("⏱ system prompt = %d chars (~%d tokens), returning=%s, memory_block=%d chars",
                len(instructions), len(instructions) // 4, intake_ctx.returning, len(block))
    # BVCTelephony cancels the agent's own echo + line noise from the inbound audio,
    # so STT/barge-in only react to the real caller (fixes the spelling cut-off while
    # KEEPING natural interruptions).
    await session.start(
        agent=agent,
        room=ctx.room,
        room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
    )
    # Scripted recording/AI disclosure (compliance), personalized only when the pack
    # warrants it. allow_interruptions=False so it always plays in full and the
    # agent's own voice during AEC warmup can't be transcribed as a phantom caller
    # turn (the "we got cut off" echo bug) — and the disclosure is never clipped.
    await session.say(_greeting_for(intake_ctx, pack), allow_interruptions=False)


def main() -> None:
    _bootstrap_env()
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME))


if __name__ == "__main__":
    main()
