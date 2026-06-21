"""Embeddings for RAG memory (OpenAI text-embedding-3-small, 1536-dim)."""

from __future__ import annotations

from openai import AsyncOpenAI

from app.config import settings


async def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        resp = await client.embeddings.create(model=settings.embedding_model, input=texts)
    finally:
        await client.close()
    return [d.embedding for d in resp.data]


async def embed_text(text: str) -> list[float]:
    return (await embed_texts([text]))[0]
