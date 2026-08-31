import hmac
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import Reminder, User
from app.schemas import DueReminderCheck, ReminderCreate

router = APIRouter(prefix="/reminders", tags=["reminders"])


@router.post("")
async def create_reminder(
    payload: ReminderCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        remind_at = datetime.fromisoformat(payload.remind_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid remind_at format")

    if remind_at <= datetime.now():
        raise HTTPException(status_code=400, detail="remind_at must be in the future")

    reminder = Reminder(
        owner_id=current_user.id,
        chat_id=payload.chat_id,
        message=payload.message,
        remind_at=remind_at,
    )
    db.add(reminder)
    await db.commit()
    await db.refresh(reminder)

    return {
        "id": str(reminder.id),
        "message": reminder.message,
        "remind_at": reminder.remind_at.isoformat(),
    }


@router.get("")
async def list_my_reminders(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.scalars(
        select(Reminder).where(Reminder.owner_id == current_user.id, Reminder.sent.is_(False))
    )
    reminders = result.all()
    return [
        {"id": str(r.id), "message": r.message, "remind_at": r.remind_at.isoformat()}
        for r in reminders
    ]


@router.post("/due")
async def fetch_and_mark_due_reminders(
    payload: DueReminderCheck,
    db: AsyncSession = Depends(get_db),
):
    """
    Internal endpoint: only the bot process (holder of BOT_SHARED_SECRET) can call
    this. It sweeps across ALL users' due reminders — this is intentionally not
    scoped to a single user's JWT, since the bot needs to deliver reminders for
    everyone. No reminder content is exposed to anyone except via this
    secret-gated call and the owning user's own /reminders (GET) endpoint.
    """
    if not settings.bot_shared_secret or not hmac.compare_digest(
        payload.bot_secret, settings.bot_shared_secret
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bot secret")

    now = datetime.now()
    result = await db.scalars(
        select(Reminder).where(Reminder.sent.is_(False), Reminder.remind_at <= now)
    )
    due = result.all()

    due_list = [{"id": str(r.id), "chat_id": r.chat_id, "message": r.message} for r in due]

    for r in due:
        r.sent = True
    await db.commit()

    return {"due": due_list}
