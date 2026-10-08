"""ساخت «جزوه‌ی کامل» از متن کامل کلاس/فایل — بدون خلاصه‌سازی.

ایده: متن رو سر جمله‌ها به تکه‌های کوچیک می‌شکنیم و هر تکه رو جدا و به ترتیب،
با پرامپتِ «وفادار» به یادداشتِ مرتب تبدیل می‌کنیم. خروجی تکه‌ها فقط پشت هم
می‌چسبن (هیچ مرحله‌ی خلاصه‌سازیِ نهایی‌ای نیست که چیزی رو حذف کنه).
"""
import asyncio
import logging
import math
import re
from typing import Callable

from app.ai.gateway import call_ai_safely, get_ai_provider

logger = logging.getLogger(__name__)

# --- تنظیمات (برای تغییر قیمت یا حجم، فقط همین‌ها رو عوض کن) ---
CHUNK_TARGET = 4000          # اندازه‌ی هدف هر تکه (حرف)
CHUNK_MAX = 4800             # هیچ تکه‌ای (به‌جز جمله‌های بدون نقطه) از این بیشتر نمی‌شه
MIN_LAST_CHUNK = 600         # تکه‌ی آخرِ خیلی کوچیک به قبلی می‌چسبه
MAX_CHARS = 150_000          # سقف متن ورودی (حدود ۳ ساعت کلاس)
CHARS_PER_CREDIT = 15_000    # هر ۱۵ هزار حرف = ۱ اعتبار
MAX_COST = 10                # سقف هزینه برای یه جزوه‌ی کامل
SECONDS_PER_PART = 15        # فقط برای تخمین زمان نمایشی
MAX_FAILED_RATIO = 0.25      # اگه بیش از این نسبت از تکه‌ها شکست بخورن، کل کار شکست خورده حساب می‌شه
RETRY_DELAYS = (0, 5, 15)    # ثانیه؛ ۳ بار تلاش برای هر تکه
MIN_OUTPUT_RATIO = 0.3       # خروجی خیلی کوتاه‌تر از ورودی یعنی مدل خلاصه کرده → دوباره تلاش

SYSTEM_PROMPT = (
    "You are an expert note-taker. You receive ONE segment of a raw lecture transcript "
    "(or study material) and turn it into complete, well-organized study notes. "
    "THIS IS NOT A SUMMARY.\n\n"
    "Rules:\n"
    "1. Keep EVERY piece of substantive content in the segment: definitions, explanations, "
    "examples, numbers, dates, names, steps, comparisons, causes and effects, and anything the "
    "teacher stresses. Do not shorten explanations into one-liners.\n"
    "2. Remove ONLY filler words, false starts, repetitions, greetings and off-topic chatter.\n"
    "3. Rewrite spoken, colloquial language into standard, formal ACADEMIC written language, keeping the original "
    "meaning and order. In Persian: replace colloquial forms with their standard written equivalents (e.g. «می‌خوام» → «می‌خواهم», "
    "«این یعنی که» → «این بدان معناست که»), use precise and concise sentences, consistent terminology, and a clean structure "
    "(headings, definitions, bullet lists). Do NOT use poetic, ornate or embellished language, and never let the rewriting "
    "change the meaning of a definition, number, example or claim.\n"
    "4. Never add facts, examples or opinions that are not in the segment. Never 'correct' the content.\n"
    "5. Text wrapped like [؟ ... ] is LOW-CONFIDENCE speech-to-text output that may be wrong. Keep it wrapped "
    "as [؟ ... ] in the notes, do NOT correct, complete or rephrase it into a confident statement, and never invent "
    "anything to fill it in. If another passage is unintelligible or looks like a transcription error, keep it and mark it with [؟].\n"
    "6. If the teacher hints at the exam ('this will be on the test', 'remember this', "
    "'important'), put that sentence on its own line starting with: ⭐ نکته‌ی امتحانی:\n"
    "7. Format as Markdown: use '## ' for a major topic and '### ' for a subtopic, only where a "
    "new topic really starts (do NOT repeat a heading when the segment continues the previous topic); "
    "use '- ' bullet lists for enumerations and steps; keep paragraphs short.\n"
    "8. Output ONLY the notes. No introduction, no closing remarks, no comments about the task.\n"
    "9. Write in the same language as the segment. If it is Persian, write EVERYTHING in Persian, "
    "including technical terms (use the standard Persian equivalent); keep Latin script only for "
    "proper nouns/acronyms with no common Persian equivalent (e.g. CBT, DSM)."
)


GUESS_RULES = (
    "\n\nGUESS MODE: the user EXPLICITLY allowed guesses for low-quality audio. For every part wrapped like [؟ ... ], "
    "you MAY propose what the speaker most likely meant, using the surrounding sentences and the lecture topic"
    "{topic}. Write each guess ONLY as [حدس: ...] at the place of the original text (replace the [؟ ... ] wrapper). "
    "Rules for guesses: (a) a guess may use general domain knowledge only to propose a likely term or phrase; "
    "(b) never invent numbers, dates, names, statistics, definitions or examples that do not appear elsewhere in the "
    "text; (c) never state a guess outside the [حدس: ...] brackets or build further claims on it; "
    "(d) if no reasonable guess exists, keep the original text wrapped as [؟ ... ]."
)


# ---------- شکستن متن ----------
_SPAN_RE = re.compile(r"\[؟[^\]]*\]")
_SPACE_HOLD = "\ue000"


def split_chunks(text: str) -> list[str]:
    """متن رو سر جمله/پاراگراف به تکه‌های حدود CHUNK_TARGET حرفی می‌شکنه، بدون اینکه حرفی گم بشه.
    بخش‌های علامت‌خورده‌ی [؟ ... ] هیچ‌وقت وسطشون بریده نمی‌شن."""
    text = (text or "").replace("\r", "")
    text = _SPAN_RE.sub(lambda m: re.sub(r"\s", _SPACE_HOLD, m.group(0)), text)  # فاصله‌های داخل علامت موقتاً قفل می‌شن
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]

    units: list[str] = []
    for paragraph in paragraphs:
        if not paragraph:
            continue
        for sentence in re.split(r"(?<=[.!?؟…])\s+", paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            while len(sentence) > CHUNK_MAX:  # جمله‌ی خیلی بلند (مثلاً متنی که نقطه نداره)
                cut = sentence.rfind(" ", 0, CHUNK_TARGET)
                if cut < CHUNK_TARGET // 2:
                    cut = CHUNK_TARGET
                units.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if sentence:
                units.append(sentence)

    chunks: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + 1 + len(unit) > CHUNK_TARGET:
            chunks.append(current)
            current = unit
        else:
            current = f"{current} {unit}" if current else unit
    if current:
        chunks.append(current)

    if len(chunks) > 1 and len(chunks[-1]) < MIN_LAST_CHUNK:
        chunks[-2] = f"{chunks[-2]} {chunks[-1]}"
        chunks.pop()
    return [c.replace(_SPACE_HOLD, " ") for c in chunks]


# ---------- قیمت و تخمین ----------
def plan(text: str) -> dict:
    """تعداد بخش‌ها، هزینه و زمان تقریبی رو برای یه متن تخمین می‌زنه."""
    text = (text or "").strip()
    chars = len(text)
    used = min(chars, MAX_CHARS)
    parts = len(split_chunks(text[:MAX_CHARS]))
    cost = min(MAX_COST, max(1, math.ceil(used / CHARS_PER_CREDIT)))
    return {
        "chars": chars,
        "used_chars": used,
        "truncated": chars > MAX_CHARS,
        "parts": parts,
        "cost": cost,
        "minutes": max(1, math.ceil(parts * SECONDS_PER_PART / 60)),
    }


def credit_spent(before: tuple, after: tuple) -> tuple[int, int]:
    """مقدار اعتبارِ مصرف‌شده رو از روی «قبل/بعد» حساب می‌کنه: (از اعتبار دائمی، از سهمیه‌ی روزانه).
    هر tuple = (credits, daily_credits_used, daily_credits_date). این‌طوری بدون دونستن جزئیات
    consume_credit می‌تونیم موقع شکست دقیقاً همون مقدار رو برگردونیم."""
    permanent = max(0, int(before[0]) - int(after[0]))
    if before[2] != after[2]:  # وسط کار روز عوض شده و سهمیه صفر شده
        daily = max(0, int(after[1]))
    else:
        daily = max(0, int(after[1]) - int(before[1]))
    return permanent, daily


# ---------- ساخت جزوه ----------
def _last_heading(markdown: str) -> str:
    for line in reversed(markdown.splitlines()):
        if line.lstrip().startswith("#"):
            return line.strip()
    return ""


async def _notes_for_chunk(
    provider, chunk: str, index: int, total: int, prev_heading: str, system: str = SYSTEM_PROMPT
) -> str | None:
    prompt = f"Segment {index + 1} of {total}."
    if prev_heading:
        prompt += (
            f" The previous segment ended under this heading: {prev_heading} "
            "If this segment continues the same topic, do not repeat the heading."
        )
    prompt += f"\n\nSegment text:\n{chunk}"

    best = ""
    for delay in RETRY_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        try:
            out = ((await call_ai_safely(provider, prompt=prompt, system=system)) or "").strip()
        except Exception:
            logger.warning("Full-notes chunk %s/%s failed (will retry)", index + 1, total, exc_info=True)
            continue
        if len(out) > len(best):
            best = out
        # خروجیِ خیلی کوتاه‌تر از ورودی یعنی مدل به‌جای جزوه، خلاصه نوشته؛ دوباره تلاش می‌کنیم
        if len(chunk) < 800 or len(out) >= MIN_OUTPUT_RATIO * len(chunk):
            return out
    return best or None  # اگه همه‌ی تلاش‌ها «کوتاه» بودن، بهترینشون رو نگه می‌داریم


async def build_full_notes(
    text: str,
    on_progress: Callable[[int, int], None] | None = None,
    guess: bool = False,
    topic: str | None = None,
) -> tuple[str, int, int]:
    """برمی‌گردونه: (متن جزوه، تعداد بخش‌های ناموفق، کل بخش‌ها).
    guess=True فقط وقتی باید باشه که کاربر صریحاً اجازه داده؛ topic (اسم درس) به حدس‌ها کمک می‌کنه."""
    chunks = split_chunks((text or "")[:MAX_CHARS])
    if not chunks:
        return "", 0, 0

    provider = get_ai_provider()
    system = SYSTEM_PROMPT
    if guess:
        system += GUESS_RULES.format(topic=f' ("{topic.strip()[:80]}")' if topic and topic.strip() else "")
    parts: list[str] = []
    failed = 0
    prev_heading = ""
    for i, chunk in enumerate(chunks):
        notes = await _notes_for_chunk(provider, chunk, i, len(chunks), prev_heading, system)
        if notes:
            parts.append(notes)
            prev_heading = _last_heading(notes) or prev_heading
        else:
            failed += 1
            parts.append("⚠️ [این بخش پردازش نشد؛ متن خام:]\n" + chunk)
        if on_progress:
            on_progress(i + 1, len(chunks))
    return "\n\n".join(parts), failed, len(chunks)
