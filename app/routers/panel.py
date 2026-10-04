"""وب‌اپ یونیمیت: صفحه‌ی اصلی + ورود با کدِ یک‌بارمصرفِ ربات (تلگرام و بله هر دو)."""
import secrets
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.security import create_access_token

router = APIRouter(prefix="/panel", tags=["panel"])

_PAGE = Path(__file__).resolve().parent.parent / "panel" / "index.html"
_FONTS = Path(__file__).resolve().parent.parent / "fonts"
_ALLOWED_FONTS = {"Vazirmatn-Regular.ttf", "Vazirmatn-Bold.ttf"}

CODE_TTL_SECONDS = 10 * 60
_CODES: dict[str, tuple[str, float]] = {}  # code -> (user_id, expires_at)


class ExchangeRequest(BaseModel):
    code: str


@router.get("", response_class=HTMLResponse)
async def page():
    return HTMLResponse(_PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@router.get("/fonts/{name}")
async def font(name: str):
    if name not in _ALLOWED_FONTS or not (_FONTS / name).exists():
        raise HTTPException(status_code=404)
    return FileResponse(_FONTS / name, media_type="font/ttf", headers={"Cache-Control": "public, max-age=604800"})


@router.post("/link")
async def create_link_code(current_user: User = Depends(get_current_user)):
    """ربات (با توکنِ خودِ کاربر) یه کد یک‌بارمصرف می‌گیره و تو لینک وب‌اپ می‌ذاره."""
    now = time.time()
    for c in [c for c, (_, exp) in _CODES.items() if exp < now]:
        _CODES.pop(c, None)
    code = secrets.token_urlsafe(24)
    _CODES[code] = (str(current_user.id), now + CODE_TTL_SECONDS)
    return {"code": code, "expires_in": CODE_TTL_SECONDS}


@router.post("/exchange")
async def exchange_code(payload: ExchangeRequest, db: AsyncSession = Depends(get_db)):
    entry = _CODES.pop(payload.code, None)  # یک‌بارمصرف
    if not entry or entry[1] < time.time():
        raise HTTPException(status_code=400, detail="لینک منقضی شده؛ از ربات دوباره بگیر.")
    user = await db.get(User, uuid.UUID(entry[0]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=400, detail="کاربر پیدا نشد.")
    return {"access_token": create_access_token(subject=str(user.id)), "token_type": "bearer"}
