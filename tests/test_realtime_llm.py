"""Realtime pipeline: DeepSeek must STREAM tokens through the LiveKit LLM
(the basis for token-by-token TTS / low latency)."""

from __future__ import annotations

import pytest

from app.config import settings


@pytest.mark.asyncio
async def test_deepseek_streams_through_livekit_llm():
    from livekit.agents.llm import ChatContext
    from livekit.plugins import openai

    llm = openai.LLM(
        model=settings.deepseek_realtime_model,
        base_url=settings.deepseek_base_url,
        api_key=settings.deepseek_api_key,
    )
    chat_ctx = ChatContext.empty()
    chat_ctx.add_message(role="user", content="Reply with a short friendly greeting.")

    chunks, text = 0, ""
    stream = llm.chat(chat_ctx=chat_ctx)
    async for ev in stream:
        delta = getattr(ev, "delta", None)
        if delta and getattr(delta, "content", None):
            chunks += 1
            text += delta.content
    await stream.aclose()

    assert text.strip(), "model returned no text"
    assert chunks >= 2, f"expected streamed tokens, got {chunks} chunk(s)"
