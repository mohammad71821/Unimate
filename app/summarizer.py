"""خلاصه‌ی کل متن (نه فقط ابتدای اون): متن بلند تکه‌تکه (سر جمله‌ها) با «همون پرامپت قبلی» خلاصه می‌شه.

پرامپت خلاصه‌سازی (SUMMARY_SYSTEM_PROMPT تو ai.py) دست‌نخورده می‌مونه و از بیرون به این ماژول داده می‌شه.
برای متن‌های کوتاه (تا ۸۰۰۰ حرف) دقیقاً مثل قبل فقط یه فراخوانی انجام می‌شه."""
import asyncio
import logging
import math

from app.ai.gateway import call_ai_safely
from app.fullnotes import split_chunks

logger = logging.getLogger(__name__)

PART_CHARS = 8000             # همون سقفی که قبلاً برای کل خلاصه بود؛ حالا سقفِ هر تکه‌ست
PART_TARGET = 7300            # هدف تکه‌بندی (کمی کمتر تا با ادغام تکه‌ی آخر از ۸۰۰۰ رد نشه)
MAX_TEXT_CHARS = 150_000      # سقف کل متن (حدود ۳ ساعت کلاس)
COMBINED_LIMIT = 9000         # اگه مجموع خلاصه‌ی تکه‌ها از این بیشتر شد، یه دور دیگه خلاصه می‌شن
MAX_REDUCE_ROUNDS = 3
CONCURRENCY = 3               # حداکثر فراخوانی هم‌زمان (برای سقف درخواست مدل‌های رایگان)
CHARS_PER_CREDIT = 30_000     # هر ۳۰ هزار حرف ۱ اعتبار؛ برای «همیشه ۱ اعتبار» عددش رو خیلی بزرگ کن
MAX_COST = 3
RETRY_DELAY_SECONDS = 3


def plan(text: str) -> dict:
    text = text or ""
    used = min(len(text), MAX_TEXT_CHARS)
    parts = 1 if used <= PART_CHARS else len(split_chunks(text[:MAX_TEXT_CHARS], PART_TARGET, PART_CHARS))
    return {
        "chars": len(text),
        "used_chars": used,
        "parts": parts,
        "cost": min(MAX_COST, max(1, math.ceil(used / CHARS_PER_CREDIT))),
    }


async def _summarize_one(provider, text: str, system: str) -> str:
    """یه فراخوانی با یه بار تلاش مجدد؛ اگه باز هم شکست خورد خطا بالا می‌ره (نباید بخشی از متن بی‌صدا گم بشه)."""
    try:
        return await call_ai_safely(provider, prompt=text, system=system)
    except Exception:
        logger.warning("Summary part failed (will retry once)", exc_info=True)
        await asyncio.sleep(RETRY_DELAY_SECONDS)
        return await call_ai_safely(provider, prompt=text, system=system)


async def _summarize_parts(provider, parts: list[str], system: str) -> list[str]:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def run(part: str) -> str:
        async with sem:
            return ((await _summarize_one(provider, part, system)) or "").strip()

    return list(await asyncio.gather(*(run(p) for p in parts)))  # ترتیب تکه‌ها حفظ می‌شه


async def summarize_long(provider, text: str, system: str) -> str:
    text = (text or "")[:MAX_TEXT_CHARS]
    if len(text) <= PART_CHARS:
        return await call_ai_safely(provider, prompt=text, system=system)  # دقیقاً مثل رفتار قبلی

    parts = split_chunks(text, PART_TARGET, PART_CHARS)
    summaries = await _summarize_parts(provider, parts, system)
    combined = "\n\n".join(s for s in summaries if s)
    rounds = 0
    while len(combined) > COMBINED_LIMIT and rounds < MAX_REDUCE_ROUNDS:
        rounds += 1  # متن خیلی بلند: خلاصه‌ی تکه‌ها دوباره با همون پرامپت خلاصه می‌شه تا خلاصه از حد بلندتر نشه
        summaries = await _summarize_parts(provider, split_chunks(combined, PART_TARGET, PART_CHARS), system)
        combined = "\n\n".join(s for s in summaries if s)
    return combined
