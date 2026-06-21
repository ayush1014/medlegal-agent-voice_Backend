"""Per-call usage + cost capture (firm-scoped).

Voice minutes come from voice_calls.duration_seconds; provider costs are
estimated from usage with default per-unit rates (the metering UI + exact
billing reconciliation are a later PRD). Recorded as an agent_event so it's
queryable per firm/lead without a new table.
"""

from __future__ import annotations

from app.agent.context import IntakeContext
from app.services import intake_service

# Estimated USD per-unit rates — override when real billing is wired.
_TWILIO_VOICE_PER_MIN = 0.0085
_DEEPGRAM_STT_PER_MIN = 0.0059
_DEEPGRAM_TTS_PER_1K_CHARS = 0.030
_DEEPSEEK_PER_1K_TOKENS = 0.001


def estimate_call_cost(
    duration_seconds: int | None, agent_chars: int, llm_tokens: int | None = None
) -> dict:
    minutes = (duration_seconds or 0) / 60
    voice = minutes * _TWILIO_VOICE_PER_MIN
    stt = minutes * _DEEPGRAM_STT_PER_MIN
    tts = (agent_chars / 1000) * _DEEPGRAM_TTS_PER_1K_CHARS
    llm = ((llm_tokens or 0) / 1000) * _DEEPSEEK_PER_1K_TOKENS
    return {
        "voice_minutes": round(minutes, 3),
        "twilio_voice": round(voice, 6),
        "deepgram_stt": round(stt, 6),
        "deepgram_tts": round(tts, 6),
        "deepseek_llm": round(llm, 6),
        "total_usd": round(voice + stt + tts + llm, 6),
        "estimate": True,
    }


async def record_usage(db, ctx: IntakeContext, breakdown: dict) -> None:
    await intake_service.log_event(db, ctx, event_type="decision", name="call_usage", payload=breakdown)
