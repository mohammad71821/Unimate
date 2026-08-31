import json
import re
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.gateway import call_ai_safely, get_ai_provider
from app.config import settings
from app.database import get_db
from app.deps import consume_credit, get_current_user
from app.flashcard_pdf import build_flashcard_pdf_bytes
from app.models import Note, User
from app.pptx_generation import MAX_SLIDES, MIN_SLIDES, build_pptx_bytes
from app.schemas import FlashcardsPdfRequest, SlidesRequest, StudyPlanRequest

router = APIRouter(prefix="/ai", tags=["ai"])

SUMMARY_SYSTEM_PROMPT = (
    "You are a helpful academic assistant. Summarize the given study material "
    "clearly and concisely, in the same language as the input text. "
    "If the input is in Persian, write the ENTIRE summary in Persian — including "
    "technical or academic terms. Never leave English words or phrases embedded "
    "in the Persian text; always use the standard Persian equivalent of technical "
    "terms (e.g. write شناخت‌ها instead of thoughts, باورها instead of beliefs). "
    "Only keep a term in Latin script if it's a proper noun/acronym with no common "
    "Persian equivalent (e.g. CBT, DSM)."
)

# متن بلندتری که مخصوص فلش‌کارت به مدل می‌فرستیم — چون هدف اینه که هیچ
# بخشی از مطلب برای ساخت فلش‌کارت حذف نشه (برخلاف خلاصه/ترجمه که سقف کوتاه‌تری کافیه)
FLASHCARDS_TEXT_LIMIT = 20000


def _flashcard_count_range(text_length: int) -> tuple[int, int]:
    """
    تعداد فلش‌کارت رو متناسب با حجم مطلب تعیین می‌کنه — مطلب کوتاه فلش‌کارت
    کم و مطلب بلند فلش‌کارت بیشتر می‌گیره، به‌جای یه بازه‌ی ثابت برای همه.
    """
    if text_length < 1200:
        return 4, 6
    if text_length < 3000:
        return 6, 10
    if text_length < 7000:
        return 10, 16
    if text_length < 14000:
        return 16, 22
    return 22, 30


PERSIAN_ONLY_RULE = ("If the input is in Persian, write EVERYTHING in Persian, including technical/academic terms. Never leave English, Russian, or other non-Persian words embedded in the Persian text; use the standard Persian equivalent for technical terms. Only keep a term in Latin script if it is a proper noun/acronym with no common Persian equivalent (e.g. CBT, DSM).")


def _build_flashcards_system_prompt(min_cards: int, max_cards: int) -> str:
    return (
        "You are a helpful academic assistant. Read the study material and generate "
        f"between {min_cards} and {max_cards} flashcards from it, in the same language as the input text. "
        "Cover the material thoroughly: if the material is long or covers many distinct sub-topics, "
        "generate more flashcards (closer to the upper end of the range) rather than skipping content — "
        "do not omit important facts just to keep the count low. If the material is short or narrow, "
        "do not pad with trivial or repetitive flashcards; generate only as many as the content genuinely "
        "supports, even if that is below the stated range. "
        f'{PERSIAN_ONLY_RULE} '
        'Respond with a JSON object of exactly this shape, and nothing else '
        '(no markdown code fences, no explanation): '
        '{"flashcards": [{"question": "...", "answer": "..."}]}'
    )


QUESTIONS_SYSTEM_PROMPT = (
    "You are a helpful academic assistant. Read the study material and generate "
    "5 multiple-choice exam questions from it, in the same language as the input text. "
    f'{PERSIAN_ONLY_RULE} '
    'Respond with a JSON object of exactly this shape, and nothing else '
    '(no markdown code fences, no explanation): '
    '{"questions": [{"question": "...", "options": ["...", "...", "...", "..."], "correct_index": 0}]}'
)

TRANSLATE_SYSTEM_PROMPT = (
    "You are a professional academic translator. Detect the language of the given text "
    "(it will be Persian or English). If it is Persian, translate it into clear, natural "
    "English. If it is English, translate it into clear, natural Persian. "
    "Preserve the meaning, structure, and any technical/academic terminology precisely. "
    "Respond with ONLY the translated text, no explanation, no language labels."
)

STUDY_PLAN_SYSTEM_PROMPT = (
    "You are an academic study-planning assistant. Given study material and the number "
    "of days available before the exam, break the material into a day-by-day study plan, "
    "in the same language as the input text. Distribute topics realistically — do not "
    "cram everything into day 1. Each day should have a short focus title and 2 to 4 "
    "concrete tasks/topics to cover. "
    'Respond with a JSON object of exactly this shape, and nothing else '
    '(no markdown code fences, no explanation): '
    '{"plan": [{"day": 1, "focus": "...", "tasks": ["...", "..."]}]}'
)


def _build_slides_system_prompt(slide_count: int) -> str:
    content_slide_count = slide_count - 1
    return (
        "You are an assistant that converts study material into a slide presentation outline, "
        "in the same language as the input text. "
        f"Produce EXACTLY {slide_count} slides in total: the first slide is a title slide for "
        f"the whole deck, followed by exactly {content_slide_count} content slides. "
        "Distribute the material evenly and logically across the content slides — do not pad "
        "with filler or repeat the same point twice just to fill slides; if the source material "
        "is thin, keep bullets concise rather than inventing content. Every content slide has a "
        "short title and 3 to 5 concise bullet points. "
        "IMPORTANT: condense and select content, but do NOT rewrite or paraphrase the wording — "
        "reuse the exact terms, names, and phrases from the source text as much as possible. "
        "Never invent, guess, or alter technical terms, names, or numbers. If unsure how to shorten "
        "a sentence without changing its meaning, keep more of the original wording rather than risk "
        "introducing an error. "
        "PERSIAN WRITING RULES (apply only if the content is in Persian/Farsi): use the Persian "
        "letterforms ی and ک (never the Arabic ي and ك); insert a zero-width non-joiner (نیم‌فاصله) "
        "in compound words and verb prefixes such as می‌روم، نمی‌دانم، کتاب‌ها، دانش‌آموز (never "
        "write them as one fused word or with a full space); use standard Persian punctuation and "
        "spacing (no space before ، ؛ : ! ؟ and one space after). "
        'Respond with a JSON object of exactly this shape, and nothing else '
        '(no markdown code fences, no explanation): '
        '{"slides": [{"title": "...", "subtitle": "..."}, '
        '{"title": "...", "bullets": ["...", "..."]}]} '
        "(only the first slide has \"subtitle\"; every following slide has \"bullets\" instead). "
        f"The \"slides\" array must contain exactly {slide_count} objects."
    )


async def _get_owned_note(note_id: uuid.UUID, current_user: User, db: AsyncSession) -> Note:
    note = await db.scalar(select(Note).where(Note.id == note_id))
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your note")
    if not note.extracted_text:
        raise HTTPException(status_code=400, detail="No extracted text available for this note")
    return note


def _strip_code_fence(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def _parse_json_object(raw: str) -> dict:
    cleaned = _strip_code_fence(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=502,
            detail={"error": "AI provider returned invalid JSON", "raw_output": raw[:2000]},
        )


@router.post("/notes/{note_id}/summarize")
async def summarize_note(
    note_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = await _get_owned_note(note_id, current_user, db)
    provider = get_ai_provider()
    summary = await call_ai_safely(provider, prompt=note.extracted_text[:8000], system=SUMMARY_SYSTEM_PROMPT)
    await consume_credit(current_user, db)
    return {"note_id": str(note.id), "summary": summary}


@router.post("/notes/{note_id}/flashcards")
async def generate_flashcards(
    note_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    فلش‌کارت‌ها رو می‌سازه و برمی‌گردونه ولی دیگه خودکار توی صف مرور ذخیره
    نمی‌کنه — کاربر باید صریحاً انتخاب کنه که به مرور اضافه بشن یا نه
    (از طریق /flashcards/save).
    """
    note = await _get_owned_note(note_id, current_user, db)
    text = note.extracted_text[:FLASHCARDS_TEXT_LIMIT]
    min_cards, max_cards = _flashcard_count_range(len(text))

    provider = get_ai_provider()
    raw = await call_ai_safely(
        provider,
        prompt=text,
        system=_build_flashcards_system_prompt(min_cards, max_cards),
        json_mode=True,
    )
    result = _parse_json_object(raw)
    cards = result.get("flashcards", [])

    credit_cost = 1 if max_cards <= 10 else (2 if max_cards <= 22 else 3)
    await consume_credit(current_user, db, amount=credit_cost)

    return {"note_id": str(note.id), "flashcards": cards}


@router.post("/notes/{note_id}/flashcards/pdf")
async def flashcards_pdf(
    note_id: uuid.UUID,
    payload: FlashcardsPdfRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    از روی فلش‌کارت‌هایی که قبلاً ساخته شدن (و کلاینت نگه‌داشته)، یه PDF
    رنگی می‌سازه. هزینه‌ی اعتبار جدیدی نداره چون ساخت محتوا قبلاً هزینه‌بر بوده.
    """
    note = await _get_owned_note(note_id, current_user, db)
    cards = [c.model_dump() for c in payload.cards]
    if not cards:
        raise HTTPException(status_code=400, detail="لیست فلش‌کارت خالیه.")

    pdf_bytes = build_flashcard_pdf_bytes(cards, title=note.original_filename)
    filename = f"{_safe_ascii_filename(note.original_filename)}-flashcards.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/notes/{note_id}/flashcards/pdf-to-chat")
async def flashcards_pdf_to_chat(
    note_id: uuid.UUID,
    payload: FlashcardsPdfRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """مثل flashcards_pdf ولی به‌جای برگردوندن فایل، مستقیم به چت تلگرام کاربر می‌فرسته (مخصوص مینی‌اپ)."""
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=500, detail="Bot token روی سرور تنظیم نشده")
    chat_id = _telegram_chat_id_from_user(current_user)
    if not chat_id:
        raise HTTPException(status_code=400, detail="این حساب به تلگرام وصل نیست")

    note = await _get_owned_note(note_id, current_user, db)
    cards = [c.model_dump() for c in payload.cards]
    if not cards:
        raise HTTPException(status_code=400, detail="لیست فلش‌کارت خالیه.")

    pdf_bytes = build_flashcard_pdf_bytes(cards, title=note.original_filename)
    filename = f"{_safe_ascii_filename(note.original_filename)}-flashcards.pdf"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendDocument",
            data={"chat_id": chat_id},
            files={"document": (filename, pdf_bytes, "application/pdf")},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="فایل ساخته شد ولی ارسالش به چت با خطا مواجه شد.")

    return {"sent": True, "filename": filename}


@router.post("/notes/{note_id}/questions")
async def generate_questions(
    note_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = await _get_owned_note(note_id, current_user, db)
    provider = get_ai_provider()
    raw = await call_ai_safely(
        provider, prompt=note.extracted_text[:8000], system=QUESTIONS_SYSTEM_PROMPT, json_mode=True
    )
    result = _parse_json_object(raw)
    await consume_credit(current_user, db)
    return {"note_id": str(note.id), "questions": result.get("questions", [])}


@router.post("/notes/{note_id}/translate")
async def translate_note(
    note_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = await _get_owned_note(note_id, current_user, db)
    provider = get_ai_provider()
    translated = await call_ai_safely(
        provider, prompt=note.extracted_text[:8000], system=TRANSLATE_SYSTEM_PROMPT
    )
    await consume_credit(current_user, db)
    return {"note_id": str(note.id), "translated_text": translated}


def _safe_ascii_filename(name: str) -> str:
    """
    Content-Disposition و multipart headerها فقط لاتین/latin-1 قبول می‌کنن.
    اسم نوت‌های متنی («متن ارسالی») یا هر اسم فارسیِ دیگه باعث کرش سرور
    می‌شد؛ این تابع یه نسخه‌ی امنِ فقط-ASCII می‌سازه.
    """
    base = name.rsplit(".", 1)[0]
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", base).strip("_")
    return safe or "note"


async def _build_slides_for_note(
    note, slide_count: int, current_user: User, db: AsyncSession
) -> tuple[bytes, str]:
    if slide_count < MIN_SLIDES or slide_count > MAX_SLIDES:
        raise HTTPException(
            status_code=400,
            detail=f"slide_count باید بین {MIN_SLIDES} و {MAX_SLIDES} باشه.",
        )

    # اسلایدهای بیشتر یعنی خروجی و کار مدل بیشتر؛ هزینه‌ی اعتبار رو متناسب می‌کنیم
    credit_cost = 2 if slide_count <= 10 else 3

    provider = get_ai_provider()
    raw = await call_ai_safely(
        provider,
        prompt=note.extracted_text[:8000],
        system=_build_slides_system_prompt(slide_count),
        json_mode=True,
    )
    outline = _parse_json_object(raw)

    try:
        pptx_bytes = build_pptx_bytes(outline)
    except ValueError:
        raise HTTPException(status_code=502, detail="خروجی هوش مصنوعی خالی بود، دوباره امتحان کن.")

    await consume_credit(current_user, db, amount=credit_cost)

    filename = f"{_safe_ascii_filename(note.original_filename)}-slides.pptx"
    return pptx_bytes, filename


@router.post("/notes/{note_id}/slides")
async def generate_slides(
    note_id: uuid.UUID,
    payload: SlidesRequest = SlidesRequest(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = await _get_owned_note(note_id, current_user, db)
    pptx_bytes, filename = await _build_slides_for_note(note, payload.slide_count, current_user, db)

    return Response(
        content=pptx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _telegram_chat_id_from_user(user: User) -> str | None:
    if user.email.startswith("tg-") and user.email.endswith("@telegram.local"):
        return user.email[len("tg-") : -len("@telegram.local")]
    return None


@router.post("/notes/{note_id}/slides/send-to-chat")
async def generate_slides_to_chat(
    note_id: uuid.UUID,
    payload: SlidesRequest = SlidesRequest(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    مخصوص مینی‌اپ: چون WebView داخلی تلگرام دانلود مستقیم فایل رو قابل‌اعتماد
    انجام نمی‌ده، این نسخه فایل رو خودش مستقیم از طریق Bot API به چتِ کاربر
    توی تلگرام می‌فرسته و فقط یه پیام موفقیت برمی‌گردونه (نه بایت‌های فایل).
    """
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=500, detail="Bot token روی سرور تنظیم نشده")

    chat_id = _telegram_chat_id_from_user(current_user)
    if not chat_id:
        raise HTTPException(status_code=400, detail="این حساب به تلگرام وصل نیست")

    note = await _get_owned_note(note_id, current_user, db)
    pptx_bytes, filename = await _build_slides_for_note(note, payload.slide_count, current_user, db)

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendDocument",
            data={"chat_id": chat_id},
            files={"document": (filename, pptx_bytes, "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="فایل ساخته شد ولی ارسالش به چت با خطا مواجه شد.")

    return {"sent": True, "filename": filename}


@router.post("/notes/{note_id}/study-plan")
async def generate_study_plan(
    note_id: uuid.UUID,
    payload: StudyPlanRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.days < 1 or payload.days > 90:
        raise HTTPException(status_code=400, detail="days must be between 1 and 90")

    note = await _get_owned_note(note_id, current_user, db)
    provider = get_ai_provider()
    prompt = (
        f"Study material:\n\n{note.extracted_text[:8000]}\n\n"
        f"Days available before the exam: {payload.days}"
    )
    raw = await call_ai_safely(provider, prompt=prompt, system=STUDY_PLAN_SYSTEM_PROMPT, json_mode=True)
    result = _parse_json_object(raw)
    await consume_credit(current_user, db)
    return {"note_id": str(note.id), "plan": result.get("plan", [])}
