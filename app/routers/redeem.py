from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import RedeemCode, RedeemCodeUse, User
from app.schemas import RedeemRequest, RedeemResult

router = APIRouter(prefix="/redeem", tags=["redeem"])


@router.post("", response_model=RedeemResult)
async def redeem_code(
    payload: RedeemRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    normalized = payload.code.strip().upper()

    redeem = await db.scalar(select(RedeemCode).where(RedeemCode.code == normalized))
    if not redeem or not redeem.is_active:
        raise HTTPException(status_code=404, detail="کد نامعتبره یا دیگه فعال نیست.")

    if redeem.times_used >= redeem.max_uses:
        raise HTTPException(status_code=400, detail="ظرفیت این کد تموم شده.")

    already_used = await db.scalar(
        select(RedeemCodeUse).where(
            RedeemCodeUse.code_id == redeem.id,
            RedeemCodeUse.user_id == current_user.id,
        )
    )
    if already_used:
        raise HTTPException(status_code=400, detail="قبلاً همین کد رو استفاده کردی.")

    if redeem.credits:
        current_user.credits += redeem.credits

    if redeem.grants_premium:
        now = datetime.now(timezone.utc)
        currently_active_premium = (
            current_user.plan == "premium"
            and (current_user.premium_until is None or current_user.premium_until > now)
        )
        if redeem.premium_days is None:
            # کد دائمیه — هر باقیمانده‌ی قبلی رو بی‌اثر می‌کنه
            current_user.premium_until = None
        else:
            base = current_user.premium_until if (currently_active_premium and current_user.premium_until) else now
            current_user.premium_until = base + timedelta(days=redeem.premium_days)
        current_user.plan = "premium"

    redeem.times_used += 1
    db.add(RedeemCodeUse(code_id=redeem.id, user_id=current_user.id))

    await db.commit()
    await db.refresh(current_user)

    parts = []
    if redeem.credits:
        parts.append(f"{redeem.credits} اعتبار اضافه شد")
    if redeem.grants_premium:
        if current_user.premium_until:
            parts.append(f"پرمیوم تا {current_user.premium_until.date().isoformat()} فعاله")
        else:
            parts.append("حساب به پرمیوم دائمی ارتقا یافت")
    message = " و ".join(parts) if parts else "کد با موفقیت فعال شد"

    return RedeemResult(
        plan=current_user.plan,
        credits=current_user.credits,
        premium_until=current_user.premium_until.isoformat() if current_user.premium_until else None,
        message=message,
    )
