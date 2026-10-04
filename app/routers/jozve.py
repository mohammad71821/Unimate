import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import fullnotes
from app.database import get_db
from app.deps import consume_credit, get_current_user
from app.jozve_export import build_jozve_docx_bytes, build_jozve_pdf_bytes
from app.models import Course, JozveItem, Note, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jozve", tags=["jozve"])

MAX_COURSE_NAME = 200
MAX_CONTENT_CHARS = 20000


class CourseCreate(BaseModel):
    name: str


class ItemCreate(BaseModel):
    course_id: uuid.UUID
    content: str
    title: str | None = None
    note_id: uuid.UUID | None = None
    number: int | None = None  # اگه ندی، شماره‌ی بعدیِ آزاد توی همون درس انتخاب می‌شه


class ItemUpdate(BaseModel):
    number: int | None = None
    title: str | None = None
    course_id: uuid.UUID | None = None  # انتقال به درس دیگه


def _auto_title(content: str) -> str | None:
    """اگه کاربر عنوان نداده، از اولین خطِ معنادارِ خلاصه یه عنوان کوتاه می‌سازیم."""
    for raw in content.splitlines():
        line = raw.strip().lstrip("#*-•> ").replace("**", "").strip()
        if line:
            return line[:60]
    return None


async def _owned_course(course_id: uuid.UUID, user: User, db: AsyncSession) -> Course:
    course = await db.scalar(select(Course).where(Course.id == course_id))
    if not course:
        raise HTTPException(status_code=404, detail="درس پیدا نشد.")
    if course.owner_id != user.id:
        raise HTTPException(status_code=403, detail="این درس مال تو نیست.")
    return course


async def _owned_item(item_id: uuid.UUID, user: User, db: AsyncSession) -> JozveItem:
    item = await db.scalar(select(JozveItem).where(JozveItem.id == item_id))
    if not item:
        raise HTTPException(status_code=404, detail="خلاصه پیدا نشد.")
    if item.owner_id != user.id:
        raise HTTPException(status_code=403, detail="این خلاصه مال تو نیست.")
    return item


async def _next_number(course_id: uuid.UUID, db: AsyncSession) -> int:
    current = await db.scalar(
        select(func.coalesce(func.max(JozveItem.number), 0)).where(JozveItem.course_id == course_id)
    )
    return int(current) + 1


def _item_out(item: JozveItem, with_content: bool = True) -> dict:
    out = {
        "id": str(item.id),
        "course_id": str(item.course_id),
        "number": item.number,
        "title": item.title,
        "kind": item.kind,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }
    if with_content:
        out["content"] = item.content
    return out


# ---------- درس‌ها ----------
@router.get("/courses")
async def list_courses(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(
        select(Course, func.count(JozveItem.id))
        .outerjoin(JozveItem, JozveItem.course_id == Course.id)
        .where(Course.owner_id == current_user.id)
        .group_by(Course.id)
        .order_by(Course.created_at)
    )
    return [
        {"id": str(c.id), "name": c.name, "items_count": n, "is_active": c.is_active}
        for c, n in rows.all()
    ]


@router.post("/courses")
async def create_course(
    payload: CourseCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = " ".join(payload.name.split())
    if not name:
        raise HTTPException(status_code=400, detail="اسم درس خالیه.")
    if len(name) > MAX_COURSE_NAME:
        raise HTTPException(status_code=400, detail=f"اسم درس بیشتر از {MAX_COURSE_NAME} حرف نشه.")

    existing = await db.scalar(
        select(Course).where(Course.owner_id == current_user.id, Course.name == name)
    )
    if existing:
        return {"id": str(existing.id), "name": existing.name, "created": False}

    course = Course(owner_id=current_user.id, name=name)
    db.add(course)
    try:
        await db.commit()
    except IntegrityError:  # دو درخواست هم‌زمان با یه اسم
        await db.rollback()
        existing = await db.scalar(
            select(Course).where(Course.owner_id == current_user.id, Course.name == name)
        )
        return {"id": str(existing.id), "name": existing.name, "created": False}
    await db.refresh(course)
    return {"id": str(course.id), "name": course.name, "created": True}


@router.get("/active")
async def get_active_course(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """درس فعالِ کاربر (اگه باشه). بات بعد از آپلود فایل صوتی این رو چک می‌کنه."""
    result = await db.scalars(
        select(Course)
        .where(Course.owner_id == current_user.id, Course.is_active.is_(True))
        .order_by(Course.created_at)
        .limit(1)
    )
    course = result.first()
    return {"course": {"id": str(course.id), "name": course.name} if course else None}


@router.post("/courses/{course_id}/activate")
async def activate_course(
    course_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    course = await _owned_course(course_id, current_user, db)
    # هر کاربر فقط یه درس فعال داره
    await db.execute(update(Course).where(Course.owner_id == current_user.id).values(is_active=False))
    course.is_active = True
    await db.commit()
    return {"id": str(course.id), "name": course.name, "is_active": True}


@router.delete("/active")
async def clear_active_course(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        update(Course)
        .where(Course.owner_id == current_user.id, Course.is_active.is_(True))
        .values(is_active=False)
    )
    await db.commit()
    return {"cleared": True}


@router.delete("/courses/{course_id}")
async def delete_course(
    course_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    course = await _owned_course(course_id, current_user, db)
    # خلاصه‌های درس هم همراهش پاک می‌شن؛ خودِ نوت‌ها و فایل‌ها دست‌نخورده می‌مونن
    items = await db.scalars(select(JozveItem).where(JozveItem.course_id == course.id))
    for item in items.all():
        await db.delete(item)
    await db.delete(course)
    await db.commit()
    return {"deleted": True}


# ---------- خلاصه‌های داخل جزوه ----------
@router.post("/items")
async def create_item(
    payload: ItemCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="متن خلاصه خالیه.")
    if len(content) > MAX_CONTENT_CHARS:
        raise HTTPException(status_code=400, detail="متن خلاصه خیلی بلنده.")

    course = await _owned_course(payload.course_id, current_user, db)

    if payload.note_id is not None:
        note = await db.scalar(select(Note).where(Note.id == payload.note_id))
        if not note or note.owner_id != current_user.id:
            raise HTTPException(status_code=404, detail="نوت پیدا نشد.")

    number = payload.number if payload.number is not None else await _next_number(course.id, db)
    if number < 1:
        raise HTTPException(status_code=400, detail="شماره باید ۱ یا بیشتر باشه.")

    title = (payload.title or "").strip() or _auto_title(content)
    item = JozveItem(
        owner_id=current_user.id,
        course_id=course.id,
        note_id=payload.note_id,
        number=number,
        title=title,
        content=content,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return {**_item_out(item, with_content=False), "course_name": course.name}


@router.get("/courses/{course_id}/items")
async def list_items(
    course_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    course = await _owned_course(course_id, current_user, db)
    result = await db.scalars(
        select(JozveItem)
        .where(JozveItem.course_id == course.id)
        .order_by(JozveItem.number, JozveItem.created_at)
    )
    return {
        "course": {"id": str(course.id), "name": course.name, "is_active": course.is_active},
        "items": [_item_out(i) for i in result.all()],
    }


@router.get("/items/{item_id}")
async def get_item(
    item_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return _item_out(await _owned_item(item_id, current_user, db))


@router.patch("/items/{item_id}")
async def update_item(
    item_id: uuid.UUID,
    payload: ItemUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    item = await _owned_item(item_id, current_user, db)

    if payload.course_id is not None and payload.course_id != item.course_id:
        new_course = await _owned_course(payload.course_id, current_user, db)
        item.course_id = new_course.id
        # توی درس جدید، شماره‌ی بعدیِ آزاد می‌گیره، مگه اینکه همزمان شماره‌ی دلخواه داده باشه
        if payload.number is None:
            item.number = await _next_number(new_course.id, db)

    if payload.number is not None:
        if payload.number < 1:
            raise HTTPException(status_code=400, detail="شماره باید ۱ یا بیشتر باشه.")
        item.number = payload.number

    if payload.title is not None:
        item.title = payload.title.strip()[:300] or None

    await db.commit()
    await db.refresh(item)
    return _item_out(item, with_content=False)


@router.delete("/items/{item_id}")
async def delete_item(
    item_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    item = await _owned_item(item_id, current_user, db)
    await db.delete(item)
    await db.commit()
    return {"deleted": True}


# ---------- خروجی ----------
@router.get("/courses/{course_id}/export")
async def export_course(
    course_id: uuid.UUID,
    format: str = "pdf",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """جزوه‌ی مرتب‌شده‌ی یه درس رو به‌صورت PDF یا DOCX برمی‌گردونه. اعتبار مصرف نمی‌کنه."""
    fmt = format.lower()
    if fmt not in ("pdf", "docx"):
        raise HTTPException(status_code=400, detail="فرمت باید pdf یا docx باشه.")

    course = await _owned_course(course_id, current_user, db)
    result = await db.scalars(
        select(JozveItem)
        .where(JozveItem.course_id == course.id)
        .order_by(JozveItem.number, JozveItem.created_at)
    )
    items = [{"number": i.number, "title": i.title, "content": i.content} for i in result.all()]
    if not items:
        raise HTTPException(status_code=400, detail="این درس هنوز هیچ خلاصه‌ای نداره.")

    if fmt == "pdf":
        data = build_jozve_pdf_bytes(course.name, items)
        media_type = "application/pdf"
    else:
        try:
            data = build_jozve_docx_bytes(course.name, items)
        except RuntimeError:
            raise HTTPException(status_code=501, detail="خروجی DOCX روی سرور فعال نیست (python-docx نصب نشده).")
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="jozve.{fmt}"'},
    )


# ---------- جزوه‌ی کامل (ساخته‌شده از کل متن، بدون خلاصه‌سازی) ----------
# کارها داخل حافظه‌ی همین پروسه نگه داشته می‌شن. اگه سرور وسط کار ری‌استارت بشه، کار نیمه‌کاره
# از بین می‌ره و چون اعتبار فقط موقع شروع کم شده، تو همون لحظه برنمی‌گرده؛ بات پیام «کار پیدا نشد» می‌ده.
_JOBS: dict[str, dict] = {}
_JOB_TASKS: set = set()
JOB_TTL_SECONDS = 2 * 60 * 60


class FullNotesCreate(BaseModel):
    note_id: uuid.UUID
    course_id: uuid.UUID


async def _owned_note_with_text(note_id: uuid.UUID, user: User, db: AsyncSession) -> Note:
    note = await db.scalar(select(Note).where(Note.id == note_id))
    if not note or note.owner_id != user.id:
        raise HTTPException(status_code=404, detail="فایل پیدا نشد.")
    if not (note.extracted_text or "").strip():
        raise HTTPException(
            status_code=400,
            detail="این فایل هنوز متنی نداره (پردازشش تموم نشده یا ناموفق بوده).",
        )
    return note


def _purge_old_jobs() -> None:
    now = time.time()
    for job_id in [j for j, v in _JOBS.items() if now - v["created"] > JOB_TTL_SECONDS]:
        _JOBS.pop(job_id, None)


def _running_job_of(user_id: str) -> dict | None:
    return next((j for j in _JOBS.values() if j["user_id"] == user_id and j["status"] == "running"), None)


async def _refund_job_credits(job: dict) -> None:
    """اعتباری که موقع شروع کم شده بود رو (همون مقدار و از همون محل) برمی‌گردونه."""
    permanent, daily = job["spent"]
    if not permanent and not daily:
        return
    agen = get_db()
    db = await agen.__anext__()
    try:
        user = await db.get(User, uuid.UUID(job["user_id"]))
        if user is None:
            return
        user.credits += permanent
        if daily and user.daily_credits_date == job["spent_date"]:
            user.daily_credits_used = max(0, user.daily_credits_used - daily)
        await db.commit()
    except Exception:
        logger.exception("Full-notes refund failed for job %s", job["id"])
    finally:
        await agen.aclose()


async def _run_full_notes_job(job: dict, text: str) -> None:
    try:
        def on_progress(done: int, total: int) -> None:
            job["done_parts"], job["total_parts"] = done, total

        notes, failed, total = await fullnotes.build_full_notes(text, on_progress)
        if not notes.strip():
            raise RuntimeError("empty output")
        if total and failed / total > fullnotes.MAX_FAILED_RATIO:
            raise RuntimeError(f"too many failed parts: {failed}/{total}")

        agen = get_db()
        db = await agen.__anext__()
        try:
            course = await db.get(Course, uuid.UUID(job["course_id"]))
            if course is None or str(course.owner_id) != job["user_id"]:
                raise RuntimeError("course is gone")
            number = await _next_number(course.id, db)
            item = JozveItem(
                owner_id=uuid.UUID(job["user_id"]),
                course_id=course.id,
                note_id=uuid.UUID(job["note_id"]),
                number=number,
                title=_auto_title(notes),
                content=notes,
                kind="full",
            )
            db.add(item)
            await db.commit()
            await db.refresh(item)
            job.update(
                status="done",
                item_id=str(item.id),
                course_id=str(course.id),
                course_name=course.name,
                number=number,
                chars=len(notes),
                failed_parts=failed,
            )
        finally:
            await agen.aclose()
    except Exception as e:
        logger.exception("Full-notes job %s failed", job["id"])
        await _refund_job_credits(job)
        job.update(
            status="failed",
            error="ساخت جزوه‌ی کامل کامل نشد و اعتبارت برگشت داده شد. چند دقیقه‌ی دیگه دوباره امتحان کن."
            if not isinstance(e, RuntimeError) or "course is gone" not in str(e)
            else "درس مقصد حذف شده بود؛ اعتبارت برگشت داده شد.",
        )


@router.get("/full-notes/estimate/{note_id}")
async def estimate_full_notes(
    note_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = await _owned_note_with_text(note_id, current_user, db)
    return fullnotes.plan(note.extracted_text)


@router.post("/full-notes")
async def start_full_notes(
    payload: FullNotesCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = await _owned_note_with_text(payload.note_id, current_user, db)
    course = await _owned_course(payload.course_id, current_user, db)

    _purge_old_jobs()
    user_id = str(current_user.id)
    if _running_job_of(user_id):
        raise HTTPException(
            status_code=409,
            detail="یه جزوه‌ی کامل دیگه‌ت هنوز در حال ساخته شدنه؛ صبر کن تموم بشه.",
        )

    text = note.extracted_text
    plan = fullnotes.plan(text)

    # اعتبار موقع شروع کم می‌شه (اگه کافی نباشه consume_credit خودش ۴۰۲ می‌ده و کاری شروع نمی‌شه)
    before = (current_user.credits, current_user.daily_credits_used, current_user.daily_credits_date)
    await consume_credit(current_user, db, amount=plan["cost"])
    await db.commit()
    after = (current_user.credits, current_user.daily_credits_used, current_user.daily_credits_date)
    spent = fullnotes.credit_spent(before, after)

    job = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "note_id": str(note.id),
        "course_id": str(course.id),
        "status": "running",
        "done_parts": 0,
        "total_parts": plan["parts"],
        "cost": plan["cost"],
        "spent": spent,
        "spent_date": after[2],
        "created": time.time(),
    }
    _JOBS[job["id"]] = job
    task = asyncio.create_task(_run_full_notes_job(job, text))
    _JOB_TASKS.add(task)
    task.add_done_callback(_JOB_TASKS.discard)

    return {
        "job_id": job["id"],
        "parts": plan["parts"],
        "cost": plan["cost"],
        "minutes": plan["minutes"],
        "truncated": plan["truncated"],
        "course_name": course.name,
    }


@router.get("/full-notes/{job_id}")
async def full_notes_status(job_id: str, current_user: User = Depends(get_current_user)):
    job = _JOBS.get(job_id)
    if not job or job["user_id"] != str(current_user.id):
        raise HTTPException(
            status_code=404,
            detail="این کار پیدا نشد (احتمالاً سرور ری‌استارت شده). اگه جزوه تو درس نیست، دوباره بسازش.",
        )
    keys = (
        "status", "done_parts", "total_parts", "cost", "error",
        "item_id", "course_id", "course_name", "number", "chars", "failed_parts",
    )
    return {k: job.get(k) for k in keys}


# ---------- خروجی یه خلاصه/جزوه‌ی تکی ----------
@router.get("/items/{item_id}/export")
async def export_item(
    item_id: uuid.UUID,
    format: str = "pdf",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    fmt = format.lower()
    if fmt not in ("pdf", "docx"):
        raise HTTPException(status_code=400, detail="فرمت باید pdf یا docx باشه.")
    item = await _owned_item(item_id, current_user, db)
    course = await db.get(Course, item.course_id)
    items = [{"number": item.number, "title": item.title, "content": item.content}]
    name = course.name if course else "جزوه"

    if fmt == "pdf":
        data = build_jozve_pdf_bytes(name, items)
        media_type = "application/pdf"
    else:
        try:
            data = build_jozve_docx_bytes(name, items)
        except RuntimeError:
            raise HTTPException(status_code=501, detail="خروجی DOCX روی سرور فعال نیست (python-docx نصب نشده).")
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="jozve-item.{fmt}"'},
    )
