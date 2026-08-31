import hmac
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import Flashcard, Note, User
from app.schemas import FlashcardOut, FlashcardReviewRequest, FlashcardReviewResult, FlashcardsSaveRequest

router = APIRouter(prefix="/flashcards", tags=["flashcards"])

DUE_BATCH_SIZE = 20


def _to_out(card: Flashcard) -> FlashcardOut:
    return FlashcardOut(
        id=card.id,
        note_id=card.note_id,
        question=card.question,
        answer=card.answer,
        repetitions=card.repetitions,
        next_review_at=card.next_review_at.isoformat(),
    )


def _apply_sm2(card: Flashcard, rating: str) -> None:
    """
    نسخه‌ی ساده‌شده‌ی الگوریتم SM-2 (همون پایه‌ی Anki) با ۳ دکمه به‌جای ۵ درجه:
    - again (یادم نبود): quality=1 → فاصله ریست به ۱ روز
    - hard  (سخت بود):    quality=3 → فاصله کم‌رشد
    - good  (بلد بودم):   quality=5 → فاصله طبق ease_factor رشد می‌کنه
    """
    quality = {"again": 1, "hard": 3, "good": 5}.get(rating, 3)

    new_ease = card.ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    card.ease_factor = max(1.3, new_ease)

    if quality < 3:
        card.repetitions = 0
        card.interval_days = 1.0
    else:
        card.repetitions += 1
        if card.repetitions == 1:
            card.interval_days = 1.0
        elif card.repetitions == 2:
            card.interval_days = 6.0
        else:
            card.interval_days = round(card.interval_days * card.ease_factor, 2)

    now = datetime.now(timezone.utc)
    card.last_reviewed_at = now
    card.next_review_at = now + timedelta(days=card.interval_days)


@router.post("/save")
async def save_flashcards(
    payload: FlashcardsSaveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    فلش‌کارت‌هایی که کاربر صریحاً انتخاب کرده رو (بعد از دیدنشون) توی صف
    مرور ذخیره می‌کنه. برخلاف قبل، ساخته‌شدن فلش‌کارت به‌تنهایی باعث ورودش
    به صف مرور نمی‌شه — این انتخابیه.
    """
    note = await db.scalar(select(Note).where(Note.id == payload.note_id))
    if not note or note.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Note not found")

    now = datetime.now(timezone.utc)
    saved = 0
    for c in payload.cards:
        question = c.question.strip()
        answer = c.answer.strip()
        if not question or not answer:
            continue
        db.add(
            Flashcard(
                owner_id=current_user.id,
                note_id=note.id,
                question=question,
                answer=answer,
                next_review_at=now,
            )
        )
        saved += 1

    if saved:
        await db.commit()

    return {"saved_to_review_deck": saved}


@router.delete("/{card_id}")
async def delete_flashcard(
    card_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """حذف یه فلش‌کارت از صف مرور، بدون این‌که لازم باشه اول مرورش کنه."""
    card = await db.get(Flashcard, card_id)
    if not card or card.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Flashcard not found")

    await db.delete(card)
    await db.commit()
    return {"deleted": True}


@router.get("/due", response_model=list[FlashcardOut])
async def get_due_flashcards(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    rows = await db.scalars(
        select(Flashcard)
        .where(Flashcard.owner_id == current_user.id, Flashcard.next_review_at <= now)
        .order_by(Flashcard.next_review_at)
        .limit(DUE_BATCH_SIZE)
    )
    return [_to_out(c) for c in rows.all()]


@router.get("/due-count")
async def get_due_count(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    count = await db.scalar(
        select(func.count(Flashcard.id)).where(
            Flashcard.owner_id == current_user.id, Flashcard.next_review_at <= now
        )
    ) or 0
    return {"due_count": count}


@router.post("/{card_id}/review", response_model=FlashcardReviewResult)
async def review_flashcard(
    card_id: uuid.UUID,
    payload: FlashcardReviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.rating not in ("again", "hard", "good"):
        raise HTTPException(status_code=400, detail="rating باید یکی از again/hard/good باشه.")

    card = await db.get(Flashcard, card_id)
    if not card or card.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Flashcard not found")

    _apply_sm2(card, payload.rating)
    await db.commit()
    await db.refresh(card)

    now = datetime.now(timezone.utc)
    remaining = await db.scalar(
        select(func.count(Flashcard.id)).where(
            Flashcard.owner_id == current_user.id, Flashcard.next_review_at <= now
        )
    ) or 0

    return FlashcardReviewResult(
        id=card.id,
        interval_days=card.interval_days,
        next_review_at=card.next_review_at.isoformat(),
        remaining_due=remaining,
    )


@router.post("/nudges/due")
async def get_nudge_targets(payload: dict, db: AsyncSession = Depends(get_db)):
    """
    Internal endpoint (bot_secret-gated) که بات یه‌بار در روز صداش می‌زنه تا
    ببینه کدوم کاربرها کارت معوقه دارن و امروز هنوز نوتیف نگرفتن. برگردوندن
    یه کاربر توی این لیست، همون‌جا last_flashcard_nudge_date رو امروز ثبت
    می‌کنه — یعنی هر کاربر در روز فقط یه‌بار نوتیف می‌گیره.
    """
    bot_secret = payload.get("bot_secret", "")
    if not settings.bot_shared_secret or not hmac.compare_digest(bot_secret, settings.bot_shared_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bot secret")

    today = date.today().isoformat()
    now = datetime.now(timezone.utc)

    due_counts = await db.execute(
        select(Flashcard.owner_id, func.count(Flashcard.id))
        .where(Flashcard.next_review_at <= now)
        .group_by(Flashcard.owner_id)
    )
    owner_to_count = dict(due_counts.all())
    if not owner_to_count:
        return {"targets": []}

    users = await db.scalars(
        select(User).where(
            User.id.in_(owner_to_count.keys()),
            User.is_active.is_(True),
            User.email.like("tg-%@telegram.local"),
        )
    )

    targets = []
    for user in users.all():
        if user.last_flashcard_nudge_date == today:
            continue
        chat_id = user.email[len("tg-") : -len("@telegram.local")]
        targets.append({"chat_id": chat_id, "due_count": owner_to_count[user.id]})
        user.last_flashcard_nudge_date = today

    await db.commit()
    return {"targets": targets}
