"""Connectivity smoke for the PRD-2 voice/AI providers.

Validates each external key with a tiny real call. Prints PASS/FAIL per provider
without leaking secrets.

    python -m scripts.check_providers
"""

from __future__ import annotations

import asyncio
import json

import httpx

from app.config import settings


async def check_deepseek() -> str:
    if not settings.deepseek_api_key:
        return "SKIP (no key)"
    from openai import OpenAI

    client = OpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)
    resp = client.chat.completions.create(
        model=settings.deepseek_model,
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        max_tokens=5,
    )
    return f"PASS (model={settings.deepseek_model}, reply={resp.choices[0].message.content!r})"


async def check_openai_embeddings() -> str:
    if not settings.openai_api_key:
        return "SKIP (no key)"
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    resp = client.embeddings.create(model=settings.embedding_model, input="hello")
    return f"PASS (dim={len(resp.data[0].embedding)})"


async def check_deepgram() -> str:
    if not settings.deepgram_api_key:
        return "SKIP (no key)"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            "https://api.deepgram.com/v1/projects",
            headers={"Authorization": f"Token {settings.deepgram_api_key}"},
        )
    if r.status_code != 200:
        return f"FAIL ({r.status_code})"
    return f"PASS ({len(r.json().get('projects', []))} project(s))"


async def check_livekit() -> str:
    if not (settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret):
        return "SKIP (incomplete config)"
    from livekit import api

    lk = api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )
    try:
        rooms = await lk.room.list_rooms(api.ListRoomsRequest())
        return f"PASS ({len(rooms.rooms)} active room(s))"
    finally:
        await lk.aclose()


async def check_gcs() -> str:
    if not (settings.gcs_bucket_name and settings.google_application_credentials_json):
        return "SKIP (incomplete config)"
    from app.services.storage import get_bucket

    # Object round-trip (what we actually need) — avoids buckets.get permission.
    blob = get_bucket().blob("_healthcheck/ping.txt")
    blob.upload_from_string("ok")
    content = blob.download_as_text()
    blob.delete()
    return f"PASS (object r/w/delete ok, read={content!r})"


async def main() -> None:
    checks = {
        "DeepSeek LLM": check_deepseek,
        "OpenAI embeddings": check_openai_embeddings,
        "Deepgram": check_deepgram,
        "LiveKit": check_livekit,
        "GCS": check_gcs,
    }
    for name, fn in checks.items():
        try:
            result = await fn()
        except Exception as e:  # noqa: BLE001 - smoke wants the message
            result = f"FAIL ({type(e).__name__}: {str(e)[:100]})"
        print(f"  {name:<20} {result}")


if __name__ == "__main__":
    asyncio.run(main())
