"""Shared OpenRouter LLM client.

All model calls in this project go through OpenRouter's OpenAI-compatible API,
which makes every model available via a single code path. The default model is a
Gemini model, but any OpenRouter model id (e.g. ``anthropic/claude-3.5-sonnet``,
``openai/gpt-4o``) can be selected through the per-stage model environment
variables without any code change.
"""

from __future__ import annotations

import os

from openai import AsyncOpenAI

OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_DEFAULT_MODEL = os.getenv("OPENROUTER_DEFAULT_MODEL", "google/gemini-2.5-pro")
# Optional metadata OpenRouter surfaces in its dashboard / rankings.
OPENROUTER_HTTP_REFERER = os.getenv("OPENROUTER_HTTP_REFERER", "")
OPENROUTER_APP_TITLE = os.getenv("OPENROUTER_APP_TITLE", "")


def get_api_key() -> str:
    return (os.getenv("OPENROUTER_API_KEY") or OPENROUTER_API_KEY).strip()


def _default_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    referer = os.getenv("OPENROUTER_HTTP_REFERER", OPENROUTER_HTTP_REFERER).strip()
    title = os.getenv("OPENROUTER_APP_TITLE", OPENROUTER_APP_TITLE).strip()
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    return headers


def get_client(api_key: str | None = None) -> AsyncOpenAI:
    key = (api_key if api_key is not None else get_api_key()).strip()
    if not key:
        raise ValueError("OPENROUTER_API_KEY environment variable is not set")
    base_url = os.getenv("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL).strip()
    return AsyncOpenAI(
        base_url=base_url,
        api_key=key,
        default_headers=_default_headers() or None,
    )


async def complete_text(client: AsyncOpenAI, model: str, prompt: str) -> str:
    """Run a single-turn chat completion and return the text content.

    No ``response_format`` is forced so that models without JSON-mode support
    still work; callers parse/repair JSON from the returned text themselves.
    """
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    if not response.choices:
        return ""
    return response.choices[0].message.content or ""
