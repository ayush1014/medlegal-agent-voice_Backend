"""DeepSeek chat model (OpenAI-compatible) for the intake agent."""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from app.config import settings

# langchain-core + pydantic 2.11 leave some forward refs (BaseCache, Callbacks)
# unresolved on ChatOpenAI; rebuild the model with them in scope so it validates.
try:  # pragma: no cover - one-time import-time fix
    from langchain_core.caches import BaseCache  # noqa: F401
    from langchain_core.callbacks import Callbacks  # noqa: F401

    ChatOpenAI.model_rebuild()
except Exception:  # noqa: BLE001
    pass


def build_chat_model(*, temperature: float = 0.3, model: str | None = None) -> ChatOpenAI:
    """LangChain chat model pointed at DeepSeek. Supports tool-calling + streaming."""
    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    return ChatOpenAI(
        model=model or settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=temperature,
    )
