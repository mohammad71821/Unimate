import uuid
from datetime import date, datetime, timezone

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

# سهمیه‌ی رایگان روزانه — هر روز از نو همین مقدار می‌شه، تجمیع نمی‌شه
DAILY_FREE_CREDITS = 10


async def verify_admin_secret(x_admin_secret: str = Header(default="")) -> None:
    """
    Guards every /admin endpoint. The panel sends the secret as a header
    (X-Admin-Secret) on each request instead of embedding it in every body.
    """
    import hmac

    if not settings.admin_secret or not hmac.compare_digest(x_admin_secret, settings.admin_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin secret")


async def ensure_premium_not_expired(user: User, db: AsyncSession) -> None:
    """
    اگه پلن کاربر premium باشه ولی premium_until گذشته باشه، خودکار به free برمی‌گردونتش.
    premium_until == None یعنی پرمیومِ دائمی (هیچ‌وقت منقضی نمی‌شه).
    """
    if user.plan != "premium" or user.premium_until is None:
        return
    if user.premium_until <= datetime.now(timezone.utc):
        user.plan = "free"
        user.premium_until = None
        await db.commit()
        await db.refresh(user)


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_error
    except JWTError:
        raise credentials_error

    user = await db.scalar(select(User).where(User.id == uuid.UUID(user_id)))
    if user is None:
        raise credentials_error
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="این حساب توسط مدیر غیرفعال شده.",
        )
    await ensure_premium_not_expired(user, db)
    return user


def _reset_daily_credits_if_new_day(user: User) -> None:
    today = date.today().isoformat()
    if user.daily_credits_date != today:
        user.daily_credits_date = today
        user.daily_credits_used = 0


async def consume_credit(user: User, db: AsyncSession, amount: int = 1) -> None:
    """
    Deducts `amount` credits for a costly (AI-gateway) action.

    Premium users are unlimited. Free-plan users first draw from today's
    daily free quota (DAILY_FREE_CREDITS, resets every day and never
    accumulates); once that's used up, they draw from their permanent
    accumulated `credits` balance (earned via referrals or manual top-up).
    Raises 402 if both are exhausted.
    """
    if user.plan == "premium":
        return

    _reset_daily_credits_if_new_day(user)

    daily_remaining = max(0, DAILY_FREE_CREDITS - user.daily_credits_used)

    if daily_remaining >= amount:
        user.daily_credits_used += amount
    else:
        from_daily = daily_remaining
        from_balance = amount - from_daily

        if user.credits < from_balance:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    "سهمیه‌ی رایگان امروزت تموم شده و اعتبار اضافه هم نداری. "
                    "فردا ۱۰ کردیت رایگان تازه می‌گیری، یا با دعوت دوستات "
                    "(هر ۳ نفر = ۱۰ کردیت همیشگی) یا خرید پلن پرمیوم می‌تونی "
                    "بیشتر استفاده کنی."
                ),
            )

        user.daily_credits_used += from_daily
        user.credits -= from_balance

    await db.commit()
