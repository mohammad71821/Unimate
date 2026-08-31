import asyncio
import logging
from functools import lru_cache

import httpx
from fastapi import HTTPException

from app.ai.base import AIProvider
from app.ai.gemini_provider import GeminiProvider
from app.ai.groq_provider import GroqProvider
from app.ai.openrouter_provider import OpenRouterProvider
from app.config import settings

logger = logging.getLogger("ai.gateway")

_PROVIDER_CLASSES: dict[str, type[AIProvider]] = {
    "openrouter": OpenRouterProvider,
    "groq": GroqProvider,
    "gemini": GeminiProvider,
}

_PROVIDER_KEYS: dict[str, str] = {
    "openrouter": settings.openrouter_api_key,
    "groq": settings.groq_api_key,
    "gemini": settings.gemini_api_key,
}

# ترتیب اولویت پشتیبان‌ها وقتی provider اصلی شکست بخوره — از باکیفیت‌ترین به
# ساده‌ترین. provider اصلی (settings.ai_provider) از این لیست حذف می‌شه چون
# قبلاً امتحان شده.
_FALLBACK_PRIORITY = ["gemini", "groq", "openrouter"]

_ai_semaphore = asyncio.Semaphore(settings.ai_max_concurrent)
_AI_QUEUE_TIMEOUT_SECONDS = 90


@lru_cache
def get_ai_provider() -> AIProvider:
    provider_cls = _PROVIDER_CLASSES.get(settings.ai_provider)
    if provider_cls is None:
        raise ValueError(f"Unknown AI provider: {settings.ai_provider}")
    return provider_cls()


@lru_cache
def _get_provider_by_name(name: str) -> AIProvider:
    return _PROVIDER_CLASSES[name]()


def _fallback_chain() -> list[AIProvider]:
    chain = []
    for name in _FALLBACK_PRIORITY:
        if name == settings.ai_provider or not _PROVIDER_KEYS.get(name):
            continue
        chain.append(_get_provider_by_name(name))
    return chain


async def call_ai_safely(
    provider: AIProvider, prompt: str, system: str | None = None, json_mode: bool = False
) -> str:
    """
    provider اصلی رو امتحان می‌کنه؛ اگه شکست خورد، به‌ترتیب بقیه‌ی
    provider های تنظیم‌شده (بر اساس _FALLBACK_PRIORITY) رو هم امتحان
    می‌کنه، تا وقتی یکی جواب بده یا همه شکست بخورن.
    """
    try:
        await asyncio.wait_for(_ai_semaphore.acquire(), timeout=_AI_QUEUE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="سرویس هوش مصنوعی الان خیلی شلوغه. چند لحظه دیگه دوباره امتحان کن.",
        )

    chain = [provider] + _fallback_chain()
    last_error: Exception | None = None

    try:
        for i, p in enumerate(chain):
            try:
                return await p.complete(prompt=prompt, system=system, json_mode=json_mode)
            except (RuntimeError, httpx.HTTPError) as e:
                last_error = e
                if i < len(chain) - 1:
                    logger.warning("AI provider %s failed, trying next: %s", type(p).__name__, e)
                continue
        logger.exception("All AI providers in the chain failed: %s", last_error)
        raise HTTPException(
            status_code=502,
            detail="سرویس هوش مصنوعی موقتاً در دسترس نیست یا خطا داد. اعتباری کسر نشد — چند لحظه دیگه دوباره امتحان کن.",
        ) from last_error
    finally:
        _ai_semaphore.release()
