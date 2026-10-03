import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.jozve_export import build_jozve_docx_bytes, build_jozve_pdf_bytes
from app.models import Course, JozveItem, Note, User

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
    return [{"id": str(c.id), "name": c.name, "items_count": n} for c, n in rows.all()]


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
        "course": {"id": str(course.id), "name": course.name},
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
