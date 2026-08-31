import hmac
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Referral, User
from app.deps import get_current_user
from app.schemas import TelegramAuthRequest, Token, UserCreate, UserLogin, UserOut
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

# هر چند نفر دعوت موفق، این‌قدر کردیت همیشگی پاداش داده می‌شه
REFERRAL_BONUS_EVERY = 3
REFERRAL_BONUS_CREDITS = 10


@router.get("/me")
async def read_me(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from datetime import date

    from app.deps import DAILY_FREE_CREDITS, _reset_daily_credits_if_new_day

    referral_count = await db.scalar(
        select(func.count(Referral.id)).where(Referral.referrer_id == current_user.id)
    ) or 0

    _reset_daily_credits_if_new_day(current_user)
    await db.commit()
    daily_remaining = max(0, DAILY_FREE_CREDITS - current_user.daily_credits_used)

    return {
        "email": current_user.email,
        "plan": current_user.plan,
        "credits": current_user.credits,
        "daily_remaining": daily_remaining,
        "daily_total": DAILY_FREE_CREDITS,
        "premium_until": current_user.premium_until.isoformat() if current_user.premium_until else None,
        "referral_count": referral_count,
    }


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(select(User).where(User.email == payload.email))
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=Token)
async def login(payload: UserLogin, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.email == payload.email))
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(subject=str(user.id))
    return Token(access_token=token)


async def _register_referral(referrer: User, referred: User, db: AsyncSession) -> None:
    """
    یه دعوت جدید رو ثبت می‌کنه و اگه دعوت‌کننده به آستانه‌ی پاداش رسیده باشه،
    کردیت همیشگی بهش اضافه می‌کنه. هر REFERRAL_BONUS_EVERY دعوت، یه پاداش.
    """
    db.add(Referral(referrer_id=referrer.id, referred_id=referred.id))
    await db.flush()

    referral_count = await db.scalar(
        select(func.count(Referral.id)).where(Referral.referrer_id == referrer.id)
    ) or 0

    if referral_count % REFERRAL_BONUS_EVERY == 0:
        referrer.credits += REFERRAL_BONUS_CREDITS

    await db.commit()


@router.post("/telegram", response_model=Token)
async def telegram_login(payload: TelegramAuthRequest, db: AsyncSession = Depends(get_db)):
    """
    Issues a token for a specific Telegram user, isolated from every other
    Telegram user. Only callable by the bot process itself, which is the only
    holder of BOT_SHARED_SECRET — end users never see or supply this secret.
    """
    if not settings.bot_shared_secret or not hmac.compare_digest(
        payload.bot_secret, settings.bot_shared_secret
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bot secret")

    synthetic_email = f"tg-{payload.telegram_user_id}@telegram.local"
    user = await db.scalar(select(User).where(User.email == synthetic_email))
    is_new_user = user is None

    if not user:
        # a random, never-shared password — this account can only ever be
        # reached through this endpoint, never through normal email/password login
        random_password = secrets.token_urlsafe(32)
        user = User(
            email=synthetic_email,
            full_name=f"Telegram User {payload.telegram_user_id}",
            hashed_password=hash_password(random_password),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    # فقط برای کاربر واقعاً تازه‌ساخته‌شده، و فقط اگه یه دعوت‌کننده‌ی معتبر
    # (متفاوت از خودش) همراه لینک آورده باشه، دعوت رو ثبت می‌کنیم
    if is_new_user and payload.referred_by and payload.referred_by != payload.telegram_user_id:
        referrer_email = f"tg-{payload.referred_by}@telegram.local"
        referrer = await db.scalar(select(User).where(User.email == referrer_email))
        if referrer:
            await _register_referral(referrer, user, db)

    token = create_access_token(subject=str(user.id))
    return Token(access_token=token)
