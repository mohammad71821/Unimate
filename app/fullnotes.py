"""ساخت «جزوه‌ی کامل» از متن کامل کلاس/فایل — بدون خلاصه‌سازی.

ایده: متن رو سر جمله‌ها به تکه‌های کوچیک می‌شکنیم و هر تکه رو جدا و به ترتیب،
با پرامپتِ «وفادار» به یادداشتِ مرتب تبدیل می‌کنیم. خروجی تکه‌ها فقط پشت هم
می‌چسبن (هیچ مرحله‌ی خلاصه‌سازیِ نهایی‌ای نیست که چیزی رو حذف کنه).
"""
import asyncio
import json
import logging
import math
import os
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
ENRICH_COST_MULTIPLIER = 1.6 # جزوه‌ی علمی (غنی‌شده) تقریباً دو برابر فراخوانی AI داره
MAX_COST_ENRICHED = 15
DEFAULT_FIELD = os.environ.get("UNIMATE_FIELD", "روانشناسی")  # رشته‌ی پیش‌فرض برای نقش «استاد راهنما»

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
    "5. Text wrapped like [؟ ... ] is speech-to-text output with low confidence. Intelligently deduce and "
    "reconstruct any unclear or garbled words based on the surrounding context and the overall subject matter of the lecture. "
    "Never leave question marks (?) or unclear placeholders in the notes; transform them into fluent, correct academic sentences.\n"
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


def split_chunks(text: str, target: int = CHUNK_TARGET, maximum: int = CHUNK_MAX) -> list[str]:
    """متن رو سر جمله/پاراگراف به تکه‌های حدود target حرفی (پیش‌فرض CHUNK_TARGET) می‌شکنه، بدون اینکه حرفی گم بشه.
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
            while len(sentence) > maximum:  # جمله‌ی خیلی بلند (مثلاً متنی که نقطه نداره)
                cut = sentence.rfind(" ", 0, target)
                if cut < target // 2:
                    cut = target
                units.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if sentence:
                units.append(sentence)

    chunks: list[str] = []
    current = ""
    for unit in units:
        if current and len(current) + 1 + len(unit) > target:
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
    base = math.ceil(used / CHARS_PER_CREDIT)
    cost = min(MAX_COST, max(1, base))
    # جزوه‌ی علمی: هر تکه دو مرحله (بازنویسی + غنی‌سازی) و یه مرحله‌ی عنوان‌گذاری آخر
    parts_enriched = 2 * parts + 1 if parts else 0
    cost_enriched = min(MAX_COST_ENRICHED, max(cost + 1, math.ceil(base * ENRICH_COST_MULTIPLIER)))
    return {
        "chars": chars,
        "used_chars": used,
        "truncated": chars > MAX_CHARS,
        "parts": parts,
        "cost": cost,
        "minutes": max(1, math.ceil(parts * SECONDS_PER_PART / 60)),
        "parts_enriched": parts_enriched,
        "cost_enriched": cost_enriched,
        "minutes_enriched": max(1, math.ceil(parts_enriched * SECONDS_PER_PART / 60)),
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


# =====================================================================================
# جزوه‌ی علمی (پایپ‌لاین سه‌مرحله‌ای): بازنویسی وفادار → غنی‌سازی علمی (جدا و علامت‌دار) → ساختار نهایی
# =====================================================================================
ENRICH_SYSTEM = (
    "You are a senior professor and researcher in the field of {field}, supervising a graduate student. "
    "You receive CLEANED LECTURE NOTES (already written down from a classroom lecture), split into numbered sections. "
    "Return ONLY a JSON object, with no markdown fences and no commentary, of exactly this shape:\n"
    '{{"sections": [{{"id": 1, "enrichment": "..."}}], '
    '"key_concepts": [{{"term": "...", "definition": "...", "source": "lecture"}}], '
    '"important_notes": [{{"kind": "exam", "text": "...", "source": "lecture"}}]}}\n\n'
    "Meaning:\n"
    "- sections[].enrichment: ONLY for sections where it genuinely helps, 1-4 short Persian sentences that explain the underlying "
    "theory or terminology using well-established, textbook-level knowledge of the field (named theorists and models, standard terms, "
    "DSM-5-TR / ICD criteria when relevant and you are sure). Skip sections that need nothing.\n"
    "- key_concepts: terms and definitions the lecturer introduced (source \"lecture\"), plus AT MOST 5 standard terms the lecture "
    "relied on without defining (source \"added\"). Definitions in Persian.\n"
    "- important_notes: kind is \"exam\" (things the lecturer stressed or hinted at; source \"lecture\") or \"clinical\" (practical or clinical "
    "implications; source \"lecture\" if the lecturer said it, otherwise \"added\").\n\n"
    "STRICT RULES:\n"
    "1. Never contradict, correct or rewrite the lecture content; you only ADD. If the lecture seems to differ from the standard view, "
    "say so politely inside that section's enrichment instead of changing anything.\n"
    "2. Add only what you are highly confident is accurate and standard in the field. If unsure, omit it.\n"
    "3. NEVER invent references: no DOIs, URLs, page numbers, statistics, study results or direct quotations. Name an author or year "
    "only if it is very well known (e.g. Piaget, Freud, Bandura, Beck).\n"
    "4. Do not repeat what the notes already say. Keep every addition brief.\n"
    "5. Text wrapped like [؟ ... ] or [حدس: ...] is unreliable speech-to-text output: do not build on it and write no enrichment for sections that contain it.\n"
    "6. If the lecture is clearly about another field, adapt to that field. Write all text in Persian."
)

TITLE_SYSTEM = (
    "Given the section headings and key terms of a university lecture, write ONE concise Persian title for the whole lecture "
    "(at most 12 words). Output only the title, with no quotes, numbering or punctuation at the end."
)

ENRICH_LABEL = "🔎 تکمیل علمی (افزوده‌ی هوش مصنوعی): "
ENRICH_LEGEND = (
    "راهنما: بخش‌های 🔎 رو هوش مصنوعی از دانش استاندارد رشته اضافه کرده و حرف استاد نیستن؛ "
    "قبل از حفظ‌کردن تأییدشون کن. بقیه‌ی متن همون مطالب کلاسه که مرتب و نوشتاری شده."
)
_UNCERTAIN_RE = re.compile(r"\[(?:؟|حدس:)")
_FABRICATION_RE = re.compile(r"(doi\b|https?://|www\.|\bet al\b|\bISBN\b|\bpp?\.\s*\d|صفحه‌ی?\s*\d)", re.I)
MAX_CONCEPTS = 40
MAX_IMPORTANT_NOTES = 30


def _parse_json(raw: str | None) -> dict | None:
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def split_sections(markdown: str) -> list[str]:
    """جزوه رو سر تیترها (# تا ######) به بخش‌ها می‌شکنه؛ الحاق دوباره‌ی بخش‌ها دقیقاً همون متن اصلیه."""
    sections: list[list[str]] = [[]]
    for line in markdown.split("\n"):
        if re.match(r"^#{1,6}\s+\S", line) and sections[-1]:
            sections.append([])
        sections[-1].append(line)
    return ["\n".join(s).strip("\n") for s in sections if "".join(s).strip()]


def demote_headings(markdown: str) -> str:
    """تیترهای بدنه یک سطح پایین‌تر می‌آن تا سه بخش اصلی جزوه سطح بالا باشن."""
    return re.sub(r"^(#{1,5})(\s)", lambda m: "#" * (len(m.group(1)) + 1) + m.group(2), markdown, flags=re.M)


def _clean_enrichment(text) -> str:
    text = " ".join(str(text or "").split())
    if not text or len(text) > 900 or _FABRICATION_RE.search(text):
        return ""  # خالی، خیلی بلند یا شبیه ارجاع ساختگی: دور ریخته می‌شه
    return text


async def _enrich_chunk(provider, notes_md: str, system: str) -> tuple[str, list, list, bool]:
    """برمی‌گردونه: (متن بخش با تکمیل‌های اضافه‌شده، مفاهیم، نکته‌ها، موفق بود؟). متن اصلی هیچ‌وقت عوض نمی‌شه."""
    sections = split_sections(notes_md)
    if not sections:
        return notes_md, [], [], True
    prompt = "Numbered sections:\n\n" + "\n\n".join(f"[{i + 1}]\n{sec[:1500]}" for i, sec in enumerate(sections))
    data = None
    for delay in (0, 5):
        if delay:
            await asyncio.sleep(delay)
        try:
            raw = await call_ai_safely(provider, prompt=prompt, system=system, json_mode=True)
        except Exception:
            logger.warning("Enrichment call failed (will retry)", exc_info=True)
            continue
        data = _parse_json(raw)
        if data is not None:
            break
    if data is None:
        return notes_md, [], [], False

    additions: dict[int, str] = {}
    for item in data.get("sections") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        text = _clean_enrichment(item.get("enrichment"))
        if text and 1 <= idx <= len(sections) and not _UNCERTAIN_RE.search(sections[idx - 1]):
            additions[idx] = text
    out = [sec + (f"\n\n{ENRICH_LABEL}{additions[i + 1]}" if (i + 1) in additions else "") for i, sec in enumerate(sections)]

    concepts = []
    for c in data.get("key_concepts") or []:
        if isinstance(c, dict) and str(c.get("term") or "").strip() and _clean_enrichment(c.get("definition")):
            concepts.append({"term": " ".join(str(c["term"]).split())[:80], "definition": _clean_enrichment(c["definition"]),
                             "added": str(c.get("source")) == "added"})
    notes = []
    for n in data.get("important_notes") or []:
        text = _clean_enrichment(n.get("text") if isinstance(n, dict) else "")
        if text:
            notes.append({"kind": "clinical" if str(n.get("kind")) == "clinical" else "exam", "text": text,
                          "added": str(n.get("source")) == "added"})
    return "\n\n".join(out), concepts, notes, True


def _merge_concepts(all_concepts: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for c in all_concepts:
        key = re.sub(r"\s+", "", c["term"]).lower()
        if key not in seen or (seen[key]["added"] and not c["added"]):
            seen[key] = c
    return list(seen.values())[:MAX_CONCEPTS]


def assemble_scientific_notes(title: str, concepts: list[dict], body: str, notes: list[dict], enriched_any: bool) -> str:
    doc = [f"# {title}"]
    if enriched_any:
        doc.append(ENRICH_LEGEND)
    if concepts:
        doc.append("## مفاهیم کلیدی و تعاریف تخصصی")
        for c in concepts:
            doc.append(f"- 🔎 {c['term']}: {c['definition']} (افزوده‌ی هوش مصنوعی)" if c["added"] else f"- {c['term']}: {c['definition']}")
    doc.append("## متن مشروح درس")
    doc.append(demote_headings(body))
    if notes:
        doc.append("## نکات مهم، بالینی و امتحانی")
        for n in notes[:MAX_IMPORTANT_NOTES]:
            label = "نکته‌ی بالینی" if n["kind"] == "clinical" else "نکته‌ی امتحانی"
            prefix = "🔎 " if n["added"] else ("⭐ " if n["kind"] == "exam" else "")
            doc.append(f"- {prefix}{label}{' (افزوده‌ی هوش مصنوعی)' if n['added'] else ''}: {n['text']}")
    return "\n\n".join(doc)


async def _make_title(provider, sections: list[str], concepts: list[dict], topic: str | None) -> str:
    heads = [m.group(1).strip() for s in sections for m in [re.match(r"^#{1,6}\s+(.+)", s)] if m][:25]
    fallback = (heads[0] if heads else (topic or "جزوه‌ی درس")).strip()
    if not heads and not concepts:
        return fallback
    prompt = "Section headings:\n" + "\n".join(f"- {h}" for h in heads) + "\n\nKey terms:\n" + "\n".join(f"- {c['term']}" for c in concepts[:15])
    try:
        title = ((await call_ai_safely(provider, prompt=prompt, system=TITLE_SYSTEM)) or "").strip()
    except Exception:
        return fallback
    title = re.sub(r"^[#\s\"«]+|[\s\"».]+$", "", title.split("\n")[0])[:120]
    return title or fallback


async def build_scientific_notes(
    text: str,
    on_progress: Callable[[int, int], None] | None = None,
    field: str | None = None,
    topic: str | None = None,
) -> tuple[str, int, int, dict]:
    chunks = split_chunks((text or "")[:MAX_CHARS])
    if not chunks:
        return "", 0, 0, {}

    provider = get_ai_provider()
    n = len(chunks)
    total_steps = 2 * n + 1
    done = 0

    def tick():
        nonlocal done
        done += 1
        if on_progress:
            on_progress(done, total_steps)

    stage1: list[tuple[str, bool]] = []
    failed = 0
    prev_heading = ""
    for i, chunk in enumerate(chunks):
        notes_md = await _notes_for_chunk(provider, chunk, i, n, prev_heading, SYSTEM_PROMPT)
        if notes_md:
            stage1.append((notes_md, True))
            prev_heading = _last_heading(notes_md) or prev_heading
        else:
            failed += 1
            stage1.append(("⚠️ [این بخش پردازش نشد؛ متن خام:]\n" + chunk, False))
        tick()

    system = ENRICH_SYSTEM.format(field=(field or DEFAULT_FIELD).strip()[:60])
    bodies: list[str] = []
    all_concepts: list[dict] = []
    all_notes: list[dict] = []
    enrich_failed = 0
    for md_text, stage1_ok in stage1:
        if stage1_ok:
            body, concepts, notes, ok = await _enrich_chunk(provider, md_text, system)
        else:
            body, concepts, notes, ok = md_text, [], [], True
        bodies.append(body)
        all_concepts += concepts
        all_notes += notes
        enrich_failed += 0 if ok else 1
        tick()

    concepts = _merge_concepts(all_concepts)
    body = "\n\n".join(bodies)
    title = await _make_title(provider, split_sections(body), concepts, topic)
    tick()
    enriched_any = ENRICH_LABEL in body or any(c["added"] for c in concepts) or any(x["added"] for x in all_notes)
    doc = assemble_scientific_notes(title, concepts, body, all_notes, enriched_any)
    return doc, failed, n, {"enrich_failed": enrich_failed, "concepts": len(concepts), "notes": len(all_notes),
                            "added_blocks": body.count(ENRICH_LABEL)}
