import hashlib
import hmac
import time
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import User
from app.security import create_access_token, hash_password
import json
import secrets

router = APIRouter(prefix="/webapp", tags=["webapp"])

INIT_DATA_MAX_AGE_SECONDS = 86400  # ۲۴ ساعت؛ بعدش initData رو منقضی‌شده حساب می‌کنیم


class WebAppAuthRequest(BaseModel):
    init_data: str
    platform: str = "telegram"  # "telegram" یا "bale" 


class WebAppAuthResponse(BaseModel):
    access_token: str
    full_name: str


def _verify_init_data(init_data: str, platform: str = "telegram") -> dict:
    """
    الگوریتم رسمی تلگرام برای تأیید initData یه Mini App (که بله هم دقیقاً
    همینو پیاده کرده، چون API‌ش سازگار با تلگرامه):
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    bot_token = settings.bale_bot_token if platform == "bale" else settings.telegram_bot_token
    if not bot_token:
        raise HTTPException(status_code=500, detail="Bot token not configured on server")

    pairs = parse_qsl(init_data, keep_blank_values=True)
    received_hash = None
    data_fields = []
    for key, value in pairs:
        if key == "hash":
            received_hash = value
        else:
            data_fields.append((key, value))

    if not received_hash:
        raise HTTPException(status_code=401, detail="initData بدون hash است")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data_fields))

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(status_code=401, detail="initData نامعتبر است")

    data = dict(data_fields)
    auth_date = int(data.get("auth_date", "0"))
    if time.time() - auth_date > INIT_DATA_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="initData منقضی شده — اپ رو ببند و دوباره باز کن")

    return data


@router.post("/auth", response_model=WebAppAuthResponse)
async def webapp_auth(payload: WebAppAuthRequest, db: AsyncSession = Depends(get_db)):
    platform = payload.platform if payload.platform in ("telegram", "bale") else "telegram"
    data = _verify_init_data(payload.init_data, platform)

    user_json = data.get("user")
    if not user_json:
        raise HTTPException(status_code=401, detail="اطلاعات کاربر توی initData نبود")
    tg_user = json.loads(user_json)
    telegram_user_id = tg_user["id"]
    display_name = tg_user.get("first_name") or f"Telegram User {telegram_user_id}"

    # دقیقاً همون قرارداد ایمیل مصنوعی‌ای که بات هم استفاده می‌کنه، تا مینی‌اپ
    # و بات به یه حساب مشترک وصل بشن (جدا برای هر پلتفرم، نه قاطی)
    account_id = f"bale-{telegram_user_id}" if platform == "bale" else str(telegram_user_id)
    synthetic_email = f"tg-{account_id}@telegram.local"
    user = await db.scalar(select(User).where(User.email == synthetic_email))

    if not user:
        random_password = secrets.token_urlsafe(32)
        user = User(
            email=synthetic_email,
            full_name=f"Telegram User {telegram_user_id}",
            hashed_password=hash_password(random_password),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    if not user.is_active:
        raise HTTPException(status_code=403, detail="این حساب توسط مدیر غیرفعال شده.")

    token = create_access_token(subject=str(user.id))
    return WebAppAuthResponse(access_token=token, full_name=display_name)


@router.get("", response_class=HTMLResponse)
async def webapp_shell():
    return WEBAPP_HTML


WEBAPP_HTML = r"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>UniMate</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://tapi.bale.ai/miniapp.js?3"></script>
<style>
  @font-face {
    font-family: 'Vazirmatn';
    src: url('https://cdn.jsdelivr.net/gh/rastikerdar/vazirmatn@v33.003/fonts/webfont/woff2/Vazirmatn-Variable.woff2') format('woff2-variations');
    font-weight: 100 900;
  }

  :root {
    --paper: #F3EEFB;
    --card-bg: #FBF8FF;
    --ink: #3B2260;
    --graphite: #7A6B95;
    --amber: #8B5FBF;
    --sage: #6FA98A;
    --rose: #C15B7A;
    --rule: #E4D9F5;
    --shadow: rgba(91, 44, 148, 0.18);
  }
  [data-theme="dark"] {
    --paper: #1E1530;
    --card-bg: #2A1F44;
    --ink: #EDE6FA;
    --graphite: #B8A9D9;
    --rule: rgba(255,255,255,0.10);
    --shadow: rgba(0,0,0,0.45);
  }

  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body {
    margin: 0; padding: 0; height: 100%;
    background: var(--paper); color: var(--ink);
    font-family: 'Vazirmatn', Tahoma, sans-serif;
    overscroll-behavior-y: contain;
    background-image: linear-gradient(45deg, var(--rule) 1px, transparent 1px), linear-gradient(-45deg, var(--rule) 1px, transparent 1px);
    background-size: 26px 26px;
  }
  [data-theme="dark"] html, [data-theme="dark"] body { background-image: linear-gradient(45deg, rgba(255,255,255,0.07) 1px, transparent 1px), linear-gradient(-45deg, rgba(255,255,255,0.07) 1px, transparent 1px); }
  #app { display: flex; flex-direction: column; min-height: 100vh; }

  /* ---------- Header ---------- */
  .topbar {
    padding: 18px 20px 12px;
    display: flex; justify-content: space-between; align-items: center;
  }
  .topbar .date { font-size: 13px; color: var(--graphite); font-weight: 500; }
  .topbar .brand-wrap { display: flex; align-items: center; gap: 8px; }
  .topbar .brand-mark {
    width: 28px; height: 28px; border-radius: 8px; background: var(--ink);
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
    box-shadow: 0 3px 8px var(--shadow);
  }
  .topbar .brand-mark span { font-size: 14px; transform: rotate(-8deg); display: block; }
  .topbar .brand { font-size: 19px; font-weight: 800; letter-spacing: -0.02em; }

  /* ---------- Tabs content area ---------- */
  .screen { flex: 1; padding: 0 18px 100px; display: none; }
  .screen.active { display: block; animation: fadeIn .25s ease; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }

  /* ---------- Bottom tab bar ---------- */
  .tabbar {
    position: fixed; bottom: 0; left: 0; right: 0;
    display: flex; justify-content: space-around;
    background: var(--card-bg);
    border-top: 1px solid var(--rule);
    padding: 10px 0 calc(10px + env(safe-area-inset-bottom));
    z-index: 20;
    box-shadow: 0 -4px 16px var(--shadow);
  }
  .tab-btn {
    display: flex; flex-direction: column; align-items: center; gap: 3px;
    background: none; border: none; color: var(--graphite);
    font-family: inherit; font-size: 11px; font-weight: 600;
    padding: 4px 18px; cursor: pointer;
  }
  .tab-btn .icon { font-size: 20px; display: inline-block; transition: transform .2s ease; }
  .tab-btn.active { color: var(--amber); }
  .tab-btn.active .icon { transform: scale(1.18) rotate(-6deg); }

  /* ---------- Floating action button (add note) ---------- */
  .fab {
    position: fixed; left: 20px; bottom: 86px; z-index: 19;
    width: 54px; height: 54px; border-radius: 50%;
    background: var(--amber); color: white; border: none;
    font-size: 26px; line-height: 54px; text-align: center;
    box-shadow: 0 8px 20px rgba(139,95,191,.45);
    display: none; cursor: pointer;
  }
  .fab.show { display: block; animation: fabIn .2s ease; }
  @keyframes fabIn { from { transform: scale(0.6); opacity: 0; } to { transform: scale(1); opacity: 1; } }

  .add-sheet-options { display: flex; flex-direction: column; gap: 10px; margin-bottom: 6px; }
  .add-option {
    display: flex; align-items: center; gap: 12px; border: 1px solid var(--rule);
    background: var(--paper); border-radius: 14px; padding: 14px; cursor: pointer;
  }
  .add-option .emoji { font-size: 22px; }
  .add-option .label { font-size: 13px; font-weight: 700; }
  .add-option .sub { font-size: 11px; color: var(--graphite); margin-top: 2px; }
  .text-note-area {
    width: 100%; min-height: 120px; border: 1px solid var(--rule); border-radius: 12px;
    padding: 10px 12px; font-family: inherit; font-size: 13px; background: var(--paper); color: var(--ink);
    margin-bottom: 10px; resize: vertical;
  }
  .text-note-submit {
    width: 100%; border: none; border-radius: 12px; padding: 12px; background: var(--ink);
    color: var(--paper); font-family: inherit; font-weight: 700; font-size: 13px; cursor: pointer;
  }

  /* ---------- Progress pill ---------- */
  .progress-pill {
    display: inline-block; margin: 4px 0 18px; padding: 5px 14px;
    background: var(--card-bg); border: 1px solid var(--rule);
    border-radius: 999px; font-size: 12px; font-weight: 600; color: var(--graphite);
  }

  /* ---------- Card stack ---------- */
  .stack { position: relative; height: 380px; margin-top: 6px; }
  .deck-ghost {
    position: absolute; inset: 0; border-radius: 20px; background: var(--card-bg);
    border: 1px solid var(--rule);
  }
  .deck-ghost.g1 { transform: translateY(10px) scale(0.97); opacity: 0.6; }
  .deck-ghost.g2 { transform: translateY(20px) scale(0.94); opacity: 0.35; }

  .card {
    position: absolute; inset: 0; border-radius: 20px; background: var(--card-bg);
    box-shadow: 0 10px 30px var(--shadow);
    perspective: 1200px;
    touch-action: pan-y;
  }
  .card-inner {
    position: relative; width: 100%; height: 100%;
    transform-style: preserve-3d; transition: transform .45s cubic-bezier(.2,.8,.2,1);
  }
  .card.flipped .card-inner { transform: rotateY(180deg); }
  .card-face {
    position: absolute; inset: 0; border-radius: 20px;
    backface-visibility: hidden; -webkit-backface-visibility: hidden;
    display: flex; flex-direction: column; padding: 26px 22px;
  }
  .card-face.back { transform: rotateY(180deg); background: var(--card-bg); }

  .holes { position: absolute; top: 10px; left: 0; right: 0; display: flex; justify-content: center; gap: 22px; }
  .holes span {
    width: 9px; height: 9px; border-radius: 50%;
    background: var(--paper); box-shadow: inset 0 1px 3px var(--shadow);
  }
  .margin-rule {
    position: absolute; top: 0; bottom: 0; right: 14%;
    width: 1px; background: var(--rose); opacity: .35;
  }
  .rule-lines {
    position: absolute; left: 22px; right: 22px; top: 120px; bottom: 26px;
    background-image: repeating-linear-gradient(to bottom, transparent, transparent 27px, var(--rule) 28px);
    opacity: .6; pointer-events: none;
  }

  .card-label { font-size: 11px; font-weight: 700; color: var(--amber); margin-top: 26px; }
  .card-text {
    flex: 1; display: flex; align-items: center; justify-content: center;
    text-align: center; font-size: 19px; font-weight: 600; line-height: 1.55;
    padding: 6px 4px; position: relative; z-index: 1;
  }
  .card-hint { text-align: center; font-size: 12px; color: var(--graphite); }

  .stamp {
    position: absolute; top: 30px; padding: 6px 16px; border-radius: 8px;
    font-size: 15px; font-weight: 800; border: 3px solid currentColor;
    transform: rotate(-12deg); opacity: 0; pointer-events: none; z-index: 5;
  }
  .stamp.good { color: var(--sage); right: 24px; }
  .stamp.again { color: var(--rose); left: 24px; }

  .rate-row { display: flex; gap: 10px; margin-top: 16px; }
  .rate-btn {
    flex: 1; border: none; border-radius: 14px; padding: 14px 8px;
    font-family: inherit; font-size: 13px; font-weight: 700; cursor: pointer;
    color: white;
  }
  .rate-btn.again { background: var(--rose); }
  .rate-btn.hard { background: var(--graphite); }
  .rate-btn.good { background: var(--sage); }

  /* ---------- Empty state ---------- */
  .empty { text-align: center; padding-top: 60px; }
  .seal {
    width: 96px; height: 96px; margin: 0 auto 18px; border-radius: 50%;
    border: 3px solid var(--amber); display: flex; align-items: center; justify-content: center;
    position: relative;
  }
  .seal::before {
    content: ""; position: absolute; inset: 8px; border: 1.5px dashed var(--amber); border-radius: 50%;
  }
  .seal span { font-size: 34px; color: var(--amber); }
  .empty h3 { font-size: 17px; margin: 0 0 6px; }
  .empty p { font-size: 13px; color: var(--graphite); margin: 0; }

  /* ---------- Notes list ---------- */
  .note-row {
    display: flex; align-items: center; gap: 12px;
    background: var(--card-bg); border: 1px solid var(--rule); border-radius: 14px;
    padding: 12px 14px; margin-bottom: 10px; cursor: pointer;
    transition: transform .12s ease;
  }
  .note-row:active { transform: scale(0.98); }
  .note-badge {
    width: 40px; height: 40px; border-radius: 12px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center; font-size: 18px;
  }
  .note-badge.pdf { background: rgba(193,91,122,.16); }
  .note-badge.image { background: rgba(111,169,138,.16); }
  .note-badge.audio { background: rgba(139,95,191,.16); }
  .note-badge.slides { background: rgba(59,34,96,.14); }
  .note-badge.text { background: var(--rule); }
  .note-row .meta { flex: 1; min-width: 0; }
  .note-row .title { font-size: 14px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .note-row .sub { font-size: 11px; color: var(--graphite); margin-top: 2px; }

  .action-chip.disabled { opacity: .35; cursor: not-allowed; }

  .sheet-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 30;
    display: none; align-items: flex-end; justify-content: center;
  }
  .sheet-overlay.show { display: flex; }
  .sheet {
    background: var(--card-bg); width: 100%; max-width: 480px;
    border-radius: 20px 20px 0 0; padding: 20px; max-height: 70vh; overflow-y: auto;
  }
  .sheet h4 { margin: 0 0 10px; font-size: 15px; }
  .sheet p { font-size: 13px; line-height: 1.7; color: var(--graphite); white-space: pre-wrap; }
  .sheet .close-sheet { display: block; margin: 14px auto 0; background: none; border: none; color: var(--amber); font-weight: 700; font-family: inherit; font-size: 13px; }

  .action-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 14px; }
  .action-chip {
    border: 1px solid var(--rule); background: var(--paper); color: var(--ink);
    border-radius: 12px; padding: 10px 4px; font-family: inherit; font-size: 12px;
    font-weight: 700; cursor: pointer;
  }
  .action-chip.active { background: var(--amber); color: white; border-color: var(--amber); }
  .sheet-result { font-size: 13px; line-height: 1.75; color: var(--graphite); white-space: pre-wrap; }
  .sheet-loading { text-align: center; padding: 20px 0; color: var(--graphite); font-size: 13px; }
  .sheet-error { color: var(--rose); font-size: 13px; text-align: center; padding: 10px 0; }

  .flashcard-preview {
    border: 1px solid var(--rule); border-radius: 12px; padding: 10px 12px; margin-bottom: 8px; background: var(--paper);
  }
  .flashcard-preview .q { font-weight: 700; font-size: 13px; margin-bottom: 4px; }
  .flashcard-preview .a { font-size: 12px; color: var(--graphite); }

  .question-card {
    border: 1px solid var(--rule); border-radius: 12px; padding: 12px; margin-bottom: 10px; background: var(--paper);
  }
  .question-card .q-text { font-weight: 700; font-size: 13px; margin-bottom: 8px; }
  .question-card .opt {
    font-size: 12px; padding: 7px 10px; border-radius: 8px; margin-bottom: 5px;
    background: var(--card-bg); border: 1px solid var(--rule); color: var(--graphite);
  }
  .question-card .opt.correct { background: rgba(91,138,114,.16); border-color: var(--sage); color: var(--sage); font-weight: 700; }

  .slide-picker { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; margin-bottom: 10px; }
  .slide-picker button {
    border: 1px solid var(--rule); background: var(--paper); color: var(--ink); border-radius: 10px;
    padding: 8px 16px; font-family: inherit; font-size: 13px; font-weight: 700; cursor: pointer;
  }

  /* ---------- Profile ---------- */
  .profile-header { text-align: center; padding: 18px 0 6px; }
  .profile-header .name { font-size: 18px; font-weight: 800; }
  .badge {
    display: inline-block; margin-top: 8px; padding: 5px 16px; border-radius: 999px;
    font-size: 12px; font-weight: 700;
  }
  .badge.premium { background: rgba(111,169,138,.18); color: var(--sage); }
  .badge.free { background: rgba(139,95,191,.18); color: var(--amber); }

  .ring-wrap { display: flex; justify-content: center; margin: 26px 0 10px; }
  .ring-num { font-size: 26px; font-weight: 800; }
  .ring-label { font-size: 11px; color: var(--graphite); }

  .redeem-box {
    margin-top: 24px; border: 1px dashed var(--rule); border-radius: 16px; padding: 16px;
    background: var(--card-bg);
  }
  .redeem-box label { font-size: 12px; font-weight: 700; color: var(--graphite); display: block; margin-bottom: 8px; }
  .redeem-row { display: flex; gap: 8px; }
  .redeem-row input {
    flex: 1; border: 1px solid var(--rule); border-radius: 10px; padding: 10px 12px;
    font-family: inherit; font-size: 14px; background: var(--paper); color: var(--ink);
  }
  .redeem-row button {
    border: none; border-radius: 10px; padding: 10px 16px; background: var(--ink); color: var(--paper);
    font-family: inherit; font-weight: 700; font-size: 13px;
  }
  .redeem-msg { font-size: 12px; margin-top: 8px; min-height: 16px; }

  .loading { text-align: center; padding-top: 80px; color: var(--graphite); font-size: 13px; }

  .topbar { position: relative; }
  .topbar::after {
    content: ""; position: absolute; left: 20px; right: 20px; bottom: 0; height: 3px;
    background: repeating-linear-gradient(90deg, var(--amber) 0 10px, transparent 10px 14px, var(--rose) 14px 24px, transparent 24px 28px);
    border-radius: 3px; opacity: .55;
  }
  .tab-btn.active .icon { text-shadow: 0 0 12px rgba(139,95,191,.55); }
  .card { transition: transform .15s ease; }
  .card:active { transform: scale(0.985); }
  .fab { background: linear-gradient(135deg, var(--amber), #6A3FA0); }
</style>
</head>
<body>
<div id="app">
  <div class="topbar">
    <div class="brand-wrap">
      <div class="brand-mark"><span>📑</span></div>
      <span class="brand">UniMate</span>
    </div>
    <span class="date" id="topDate"></span>
  </div>

  <div id="loading" class="loading">در حال بارگذاری...</div>

  <div class="screen" id="screen-review">
    <div id="reviewContent"></div>
  </div>

  <div class="screen" id="screen-notes">
    <div id="notesContent"></div>
  </div>

  <div class="screen" id="screen-profile">
    <div id="profileContent"></div>
  </div>

  <div class="tabbar">
    <button class="tab-btn active" data-tab="review"><span class="icon">🔁</span>مرور</button>
    <button class="tab-btn" data-tab="notes"><span class="icon">📚</span>نوت‌ها</button>
    <button class="tab-btn" data-tab="profile"><span class="icon">👤</span>من</button>
  </div>

  <button class="fab" id="fabAdd" onclick="openAddSheet()">+</button>
  <input type="file" id="fileInput" style="display:none" onchange="handleFileSelected(event)">
</div>

<div class="sheet-overlay" id="addSheetOverlay">
  <div class="sheet">
    <h4>افزودن نوت جدید</h4>
    <div id="addSheetBody">
      <div class="add-sheet-options">
        <div class="add-option" onclick="document.getElementById('fileInput').click()">
          <span class="emoji">📎</span>
          <div>
            <div class="label">آپلود فایل</div>
            <div class="sub">PDF، عکس جزوه، ویس، یا PPTX</div>
          </div>
        </div>
        <div class="add-option" onclick="openTextNoteForm()">
          <span class="emoji">✍️</span>
          <div>
            <div class="label">متن دلخواه</div>
            <div class="sub">مستقیم یه متن بنویس یا پیست کن</div>
          </div>
        </div>
      </div>
    </div>
    <button class="close-sheet" onclick="closeAddSheet()">بستن</button>
  </div>
</div>

<div class="sheet-overlay" id="sheetOverlay">
  <div class="sheet">
    <h4 id="sheetTitle"></h4>
    <div class="action-grid" id="sheetActions">
      <button class="action-chip" onclick="runNoteAction('text')">📄 متن</button>
      <button class="action-chip" onclick="runNoteAction('summary')">📝 خلاصه</button>
      <button class="action-chip" onclick="runNoteAction('flashcards')">🃏 فلش‌کارت</button>
      <button class="action-chip" onclick="runNoteAction('questions')">❓ سؤالات</button>
      <button class="action-chip" onclick="runNoteAction('translate')">🌐 ترجمه</button>
      <button class="action-chip" onclick="runNoteAction('slides')">🎬 اسلاید</button>
    </div>
    <div id="sheetBody"></div>
    <button class="close-sheet" onclick="closeSheet()">بستن</button>
  </div>
</div>

<script>
const PLATFORM = (window.Bale && window.Bale.WebApp) ? 'bale' : 'telegram';
const tg = PLATFORM === 'bale' ? window.Bale.WebApp : (window.Telegram && window.Telegram.WebApp);
if (tg) {
  tg.ready();
  tg.expand();
  document.documentElement.dataset.theme = tg.colorScheme === 'dark' ? 'dark' : 'light';
  if (tg.onEvent) {
    tg.onEvent('themeChanged', () => {
      document.documentElement.dataset.theme = tg.colorScheme === 'dark' ? 'dark' : 'light';
    });
  }
}

let TOKEN = null;
let DUE_QUEUE = [];
let DUE_INDEX = 0;
let REVIEWED_TODAY = 0;

const PERSIAN_WEEKDAYS = ['یکشنبه','دوشنبه','سه‌شنبه','چهارشنبه','پنجشنبه','جمعه','شنبه'];

function setTopDate() {
  const d = new Date();
  document.getElementById('topDate').textContent = PERSIAN_WEEKDAYS[d.getDay()];
}

async function api(path, method = 'GET', body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (TOKEN) opts.headers['Authorization'] = 'Bearer ' + TOKEN;
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || 'خطا');
  }
  return resp.json();
}

async function authenticate() {
  const initData = tg ? tg.initData : '';
  const res = await api('/webapp/auth', 'POST', { init_data: initData, platform: PLATFORM });
  TOKEN = res.access_token;
  return res;
}

function switchTab(name) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('screen-' + name).classList.add('active');
  document.querySelector(`.tab-btn[data-tab="${name}"]`).classList.add('active');
  document.getElementById('fabAdd').classList.toggle('show', name === 'notes');
  if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
  if (name === 'notes') loadNotes();
  if (name === 'profile') loadProfile();
}

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

/* ---------------- Review tab ---------------- */
async function loadReview() {
  const container = document.getElementById('reviewContent');
  try {
    DUE_QUEUE = await api('/flashcards/due');
  } catch (e) {
    container.innerHTML = `<div class="empty"><p>مشکلی توی دریافت کارت‌ها پیش اومد: ${e.message}</p></div>`;
    return;
  }
  DUE_INDEX = 0;
  renderReviewCard();
}

function renderReviewCard() {
  const container = document.getElementById('reviewContent');
  if (DUE_INDEX >= DUE_QUEUE.length) {
    container.innerHTML = `
      <div class="empty">
        <div class="seal"><span>✓</span></div>
        <h3>${REVIEWED_TODAY > 0 ? 'مرورت تموم شد!' : 'امروز کاری نداری'}</h3>
        <p>${REVIEWED_TODAY > 0 ? REVIEWED_TODAY + ' کارت مرور کردی 👏' : 'برای مرور جدید، اول توی بات یه فلش‌کارت بساز.'}</p>
      </div>`;
    return;
  }

  const card = DUE_QUEUE[DUE_INDEX];
  container.innerHTML = `
    <div class="progress-pill">${DUE_INDEX + 1} از ${DUE_QUEUE.length}</div>
    <div class="stack">
      <div class="deck-ghost g2"></div>
      <div class="deck-ghost g1"></div>
      <div class="card" id="activeCard">
        <div class="stamp good">بلد بودم</div>
        <div class="stamp again">یادم نبود</div>
        <div class="card-inner" id="cardInner">
          <div class="card-face front">
            <div class="holes"><span></span><span></span><span></span><span></span></div>
            <div class="margin-rule"></div>
            <div class="card-label">سؤال</div>
            <div class="card-text">${escapeHtml(card.question)}</div>
            <div class="card-hint">برای دیدن جواب ضربه بزن</div>
          </div>
          <div class="card-face back">
            <div class="holes"><span></span><span></span><span></span><span></span></div>
            <div class="margin-rule"></div>
            <div class="rule-lines"></div>
            <div class="card-label">جواب</div>
            <div class="card-text">${escapeHtml(card.answer)}</div>
          </div>
        </div>
      </div>
    </div>
    <div class="rate-row" id="rateRow" style="visibility:hidden;">
      <button class="rate-btn again" onclick="rate('again')">❌ یادم نبود</button>
      <button class="rate-btn hard" onclick="rate('hard')">🤔 سخت بود</button>
      <button class="rate-btn good" onclick="rate('good')">✅ بلد بودم</button>
    </div>
  `;
  attachSwipeHandlers();
}

function attachSwipeHandlers() {
  const cardEl = document.getElementById('activeCard');
  const inner = document.getElementById('cardInner');
  let flipped = false;
  let startX = 0, currentX = 0, dragging = false;

  cardEl.addEventListener('click', (e) => {
    if (dragging) return;
    if (!flipped) {
      flipped = true;
      cardEl.classList.add('flipped');
      document.getElementById('rateRow').style.visibility = 'visible';
      if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
    }
  });

  cardEl.addEventListener('touchstart', (e) => {
    if (!flipped) return;
    startX = e.touches[0].clientX;
    dragging = true;
  });
  cardEl.addEventListener('touchmove', (e) => {
    if (!dragging) return;
    currentX = e.touches[0].clientX - startX;
    inner.style.transition = 'none';
    inner.style.transform = `rotateY(180deg) translateX(${currentX}px) rotate(${currentX / 20}deg)`;
    const goodStamp = cardEl.querySelector('.stamp.good');
    const againStamp = cardEl.querySelector('.stamp.again');
    goodStamp.style.opacity = Math.max(0, currentX / 100);
    againStamp.style.opacity = Math.max(0, -currentX / 100);
  });
  cardEl.addEventListener('touchend', () => {
    if (!dragging) return;
    dragging = false;
    inner.style.transition = '';
    if (currentX > 90) { rate('good'); }
    else if (currentX < -90) { rate('again'); }
    else {
      inner.style.transform = 'rotateY(180deg)';
      cardEl.querySelector('.stamp.good').style.opacity = 0;
      cardEl.querySelector('.stamp.again').style.opacity = 0;
    }
    currentX = 0;
  });
}

async function rate(rating) {
  const card = DUE_QUEUE[DUE_INDEX];
  if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(rating === 'again' ? 'warning' : 'success');
  try {
    await api(`/flashcards/${card.id}/review`, 'POST', { rating });
  } catch (e) { /* بی‌صدا رد شو، مرور رو متوقف نکن */ }
  REVIEWED_TODAY += 1;
  DUE_INDEX += 1;
  renderReviewCard();
}

async function saveFlashcardsToReview(btnEl) {
  if (!CURRENT_FLASHCARDS.length) return;
  btnEl.disabled = true;
  btnEl.textContent = 'در حال ذخیره...';
  try {
    const res = await api('/flashcards/save', 'POST', { note_id: CURRENT_NOTE_ID, cards: CURRENT_FLASHCARDS });
    btnEl.textContent = `✅ ${res.saved_to_review_deck} کارت اضافه شد`;
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  } catch (e) {
    btnEl.textContent = '❌ خطا';
    btnEl.disabled = false;
  }
}

async function sendFlashcardsPdfToChat(btnEl) {
  if (!CURRENT_FLASHCARDS.length) return;
  btnEl.disabled = true;
  btnEl.textContent = 'در حال ساخت PDF...';
  try {
    await api(`/ai/notes/${CURRENT_NOTE_ID}/flashcards/pdf-to-chat`, 'POST', { cards: CURRENT_FLASHCARDS });
    btnEl.textContent = '✅ به چت فرستاده شد';
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  } catch (e) {
    btnEl.textContent = '❌ خطا';
    btnEl.disabled = false;
  }
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str || '';
  return div.innerHTML;
}

function questionCardHtml(q, index) {
  const options = q.options || [];
  const correctIdx = typeof q.correct_index === 'number' ? q.correct_index : -1;
  const optsHtml = options
    .map((opt, i) => `<div class="opt ${i === correctIdx ? 'correct' : ''}">${escapeHtml(opt)}</div>`)
    .join('');
  return `
    <div class="question-card">
      <div class="q-text">${index + 1}. ${escapeHtml(q.question || '')}</div>
      ${optsHtml}
    </div>`;
}

/* ---------------- Notes tab ---------------- */
async function loadNotes() {
  const container = document.getElementById('notesContent');
  container.innerHTML = '<div class="loading">در حال بارگذاری...</div>';
  try {
    const notes = await api('/notes/');
    if (!notes.length) {
      container.innerHTML = `<div class="empty"><div class="seal"><span>+</span></div><h3>هنوز نوتی نداری</h3><p>با دکمه‌ی 🟠 پایین چپ یه فایل آپلود کن یا متن بنویس.</p></div>`;
      return;
    }
    container.innerHTML = notes.map(n => {
      const info = noteTypeInfo(n.content_type, n.original_filename);
      return `
      <div class="note-row" onclick='openNote(${JSON.stringify(n.id)}, ${JSON.stringify(n.original_filename)}, ${JSON.stringify(n.content_type)})'>
        <div class="note-badge ${info.cls}">${info.emoji}</div>
        <div class="meta">
          <div class="title">${escapeHtml(n.original_filename)}</div>
          <div class="sub">${(n.created_at || '').split('T')[0]}</div>
        </div>
      </div>
    `;
    }).join('');
  } catch (e) {
    container.innerHTML = `<div class="empty"><p>${e.message}</p></div>`;
  }
}

function noteTypeInfo(contentType, filename) {
  const ct = (contentType || '').toLowerCase();
  const name = (filename || '').toLowerCase();
  if (ct.includes('pdf') || name.endsWith('.pdf')) return { cls: 'pdf', emoji: '📕' };
  if (ct.includes('image') || /\.(jpg|jpeg|png|webp)$/.test(name)) return { cls: 'image', emoji: '🖼️' };
  if (ct.includes('audio') || /\.(ogg|mp3|m4a|wav)$/.test(name)) return { cls: 'audio', emoji: '🎙️' };
  if (ct.includes('presentation') || name.endsWith('.pptx')) return { cls: 'slides', emoji: '🎬' };
  if (ct.includes('text')) return { cls: 'text', emoji: '✍️' };
  return { cls: 'text', emoji: '📄' };
}

let CURRENT_NOTE_ID = null;
let CURRENT_NOTE_NAME = '';
let CURRENT_FLASHCARDS = [];

async function sendTextToChat(title, text, btnEl) {
  btnEl.disabled = true;
  btnEl.textContent = 'در حال ارسال...';
  try {
    await api('/notes/send-text-to-chat', 'POST', { title, text });
    btnEl.textContent = '✅ به چت فرستاده شد';
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  } catch (e) {
    btnEl.textContent = '❌ ' + e.message;
    btnEl.disabled = false;
  }
}

function openNote(noteId, filename, contentType) {
  CURRENT_NOTE_ID = noteId;
  CURRENT_NOTE_NAME = filename;
  document.getElementById('sheetTitle').textContent = filename;
  document.getElementById('sheetBody').innerHTML = '<p class="sheet-result">یه عملیات رو از بالا انتخاب کن.</p>';

  const chips = document.querySelectorAll('.action-chip');
  chips.forEach(b => b.classList.remove('active'));

  const isPlainText = (contentType || '').toLowerCase().includes('text');
  const textChip = chips[0]; // اولین دکمه، «متن»ه
  if (textChip) {
    textChip.classList.toggle('disabled', isPlainText);
    textChip.disabled = isPlainText;
    textChip.title = isPlainText ? 'این نوت خودش یه متنه، همینه که هست' : '';
  }

  document.getElementById('sheetOverlay').classList.add('show');
}

function closeSheet() {
  document.getElementById('sheetOverlay').classList.remove('show');
  CURRENT_NOTE_ID = null;
}

async function runNoteAction(action) {
  if (!CURRENT_NOTE_ID) return;
  const chips = document.querySelectorAll('.action-chip');
  const idx = ['text', 'summary', 'flashcards', 'questions', 'translate', 'slides'].indexOf(action);
  if (chips[idx] && chips[idx].disabled) return;
  chips.forEach(b => b.classList.remove('active'));
  if (chips[idx]) chips[idx].classList.add('active');

  const body = document.getElementById('sheetBody');

  if (action === 'slides') {
    body.innerHTML = `
      <div class="slide-picker">
        <button onclick="downloadSlides(5)">۵</button>
        <button onclick="downloadSlides(10)">۱۰</button>
        <button onclick="downloadSlides(15)">۱۵</button>
        <button onclick="downloadSlides(20)">۲۰</button>
      </div>
      <p class="sheet-result" style="text-align:center;">تعداد اسلاید رو انتخاب کن.</p>`;
    return;
  }

  body.innerHTML = '<div class="sheet-loading">در حال پردازش... ⏳</div>';

  try {
    if (action === 'text') {
      const note = await api(`/notes/${CURRENT_NOTE_ID}`);
      const text = note.extracted_text || 'متنی برای این فایل ثبت نشده.';
      body.innerHTML = `<p class="sheet-result">${escapeHtml(text)}</p>`;
      return;
    }

    if (action === 'summary') {
      const res = await api(`/ai/notes/${CURRENT_NOTE_ID}/summarize`, 'POST');
      body.innerHTML = `<p class="sheet-result">${escapeHtml(res.summary)}</p>
        <button class="text-note-submit" style="margin-top:10px;" onclick="sendTextToChat('خلاصه', ${JSON.stringify(res.summary)}, this)">📩 ارسال به چت تلگرام</button>`;
      return;
    }

    if (action === 'questions') {
      const res = await api(`/ai/notes/${CURRENT_NOTE_ID}/questions`, 'POST');
      const questions = res.questions || [];
      const plain = questions.map((q, i) =>
        `${i+1}. ${q.question}\n` + (q.options||[]).map((o,j)=>`   ${j}) ${o}`).join('\n') + `\n   پاسخ درست: گزینه ${q.correct_index}`
      ).join('\n\n');
      body.innerHTML = (questions.length
        ? questions.map((q, i) => questionCardHtml(q, i)).join('')
        : '<p class="sheet-result">سؤالی ساخته نشد.</p>')
        + (questions.length ? `<button class="text-note-submit" style="margin-top:10px;" onclick="sendTextToChat('سؤالات تستی', ${JSON.stringify('')} || ${JSON.stringify(plain)}, this)">📩 ارسال به چت تلگرام</button>` : '');
      return;
    }

    if (action === 'translate') {
      const res = await api(`/ai/notes/${CURRENT_NOTE_ID}/translate`, 'POST');
      body.innerHTML = `<p class="sheet-result">${escapeHtml(res.translated_text)}</p>
        <button class="text-note-submit" style="margin-top:10px;" onclick="sendTextToChat('ترجمه', ${JSON.stringify(res.translated_text)}, this)">📩 ارسال به چت تلگرام</button>`;
      return;
    }

    if (action === 'flashcards') {
      const res = await api(`/ai/notes/${CURRENT_NOTE_ID}/flashcards`, 'POST');
      CURRENT_FLASHCARDS = res.flashcards || [];
      const cardsHtml = CURRENT_FLASHCARDS.map(c => `
        <div class="flashcard-preview">
          <div class="q">${escapeHtml(c.question)}</div>
          <div class="a">${escapeHtml(c.answer)}</div>
        </div>`).join('');
      body.innerHTML = `
        <p class="sheet-result" style="text-align:center; font-weight:700;">${CURRENT_FLASHCARDS.length} فلش‌کارت ساخته شد</p>
        <div style="display:flex; gap:8px; margin:10px 0;">
          <button class="text-note-submit" style="flex:1;" onclick="saveFlashcardsToReview(this)">➕ افزودن به مرور</button>
          <button class="text-note-submit" style="flex:1; background:var(--sage);" onclick="sendFlashcardsPdfToChat(this)">📄 PDF به چت</button>
        </div>
      ` + cardsHtml;
      return;
    }
  } catch (e) {
    body.innerHTML = `<div class="sheet-error">${escapeHtml(e.message)}</div>`;
  }
}

async function downloadSlides(count) {
  const body = document.getElementById('sheetBody');
  body.innerHTML = '<div class="sheet-loading">در حال ساخت فایل اسلاید... ⏳</div>';
  try {
    const res = await api(`/ai/notes/${CURRENT_NOTE_ID}/slides/send-to-chat`, 'POST', { slide_count: count });
    body.innerHTML =
      '<p class="sheet-result" style="color:var(--sage); font-weight:700; text-align:center;">' +
      '✅ فایل اسلاید ساخته شد و توی چت باتت فرستاده شد — برو تلگرام رو چک کن.</p>';
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
  } catch (e) {
    body.innerHTML = `<div class="sheet-error">${escapeHtml(e.message)}</div>`;
  }
}

/* ---------------- Add note (upload / text) ---------------- */
function openAddSheet() {
  document.getElementById('addSheetBody').innerHTML = `
    <div class="add-sheet-options">
      <div class="add-option" onclick="document.getElementById('fileInput').click()">
        <span class="emoji">📎</span>
        <div>
          <div class="label">آپلود فایل</div>
          <div class="sub">PDF، عکس جزوه، ویس، یا PPTX</div>
        </div>
      </div>
      <div class="add-option" onclick="openTextNoteForm()">
        <span class="emoji">✍️</span>
        <div>
          <div class="label">متن دلخواه</div>
          <div class="sub">مستقیم یه متن بنویس یا پیست کن</div>
        </div>
      </div>
    </div>`;
  document.getElementById('addSheetOverlay').classList.add('show');
}
function closeAddSheet() { document.getElementById('addSheetOverlay').classList.remove('show'); }

function openTextNoteForm() {
  document.getElementById('addSheetBody').innerHTML = `
    <textarea class="text-note-area" id="textNoteInput" placeholder="متن رو اینجا بنویس یا پیست کن..."></textarea>
    <button class="text-note-submit" onclick="submitTextNote()">ثبت به‌عنوان نوت جدید</button>
    <div class="redeem-msg" id="textNoteMsg"></div>`;
}

async function submitTextNote() {
  const textarea = document.getElementById('textNoteInput');
  const msg = document.getElementById('textNoteMsg');
  const text = textarea.value.trim();
  if (!text) return;
  msg.style.color = 'var(--graphite)';
  msg.textContent = 'در حال ثبت...';
  try {
    await api('/notes/from-text', 'POST', { text });
    closeAddSheet();
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
    loadNotes();
  } catch (e) {
    msg.style.color = 'var(--rose)';
    msg.textContent = e.message;
  }
}

async function handleFileSelected(event) {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;

  document.getElementById('addSheetBody').innerHTML = '<div class="sheet-loading">در حال آپلود و پردازش... ⏳</div>';
  document.getElementById('addSheetOverlay').classList.add('show');

  try {
    const formData = new FormData();
    formData.append('file', file);
    const resp = await fetch('/notes/upload', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + TOKEN },
      body: formData,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || 'خطا در آپلود');
    }
    closeAddSheet();
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
    loadNotes();
  } catch (e) {
    document.getElementById('addSheetBody').innerHTML = `<div class="sheet-error">${escapeHtml(e.message)}</div>`;
  }
}


/* ---------------- Profile tab ---------------- */
async function loadProfile(nameFromAuth) {
  const container = document.getElementById('profileContent');
  container.innerHTML = '<div class="loading">در حال بارگذاری...</div>';
  try {
    const me = await api('/auth/me');
    const isPremium = me.plan === 'premium';
    const badge = isPremium
      ? `<span class="badge premium">پرمیوم${me.premium_until ? ' تا ' + me.premium_until.split('T')[0] : ' (دائمی)'}</span>`
      : `<span class="badge free">رایگان</span>`;

    let ringHtml = '';
    if (!isPremium) {
      const pct = Math.max(0, Math.min(100, (me.credits / 20) * 100));
      ringHtml = `
        <div class="ring-wrap">
          ${svgRing(pct, me.credits)}
        </div>
        <div style="text-align:center;" class="ring-label">اعتبار باقی‌مانده</div>`;
    }

    container.innerHTML = `
      <div class="profile-header">
        <div class="name">${escapeHtml(window.PROFILE_NAME || 'دانشجو')}</div>
        ${badge}
      </div>
      ${ringHtml}
      <div class="redeem-box">
        <label>فعال‌سازی کد شارژ / پرمیوم</label>
        <div class="redeem-row">
          <input id="redeemInput" placeholder="UM-XXXX-XXXX" />
          <button onclick="submitRedeem()">فعال کن</button>
        </div>
        <div class="redeem-msg" id="redeemMsg"></div>
      </div>
    `;
  } catch (e) {
    container.innerHTML = `<div class="empty"><p>${e.message}</p></div>`;
  }
}

function svgRing(pct, num) {
  const r = 46, c = 2 * Math.PI * r;
  const offset = c * (1 - pct / 100);
  return `
    <svg width="120" height="120" viewBox="0 0 120 120">
      <circle cx="60" cy="60" r="${r}" fill="none" stroke="var(--rule)" stroke-width="10"/>
      <circle cx="60" cy="60" r="${r}" fill="none" stroke="var(--amber)" stroke-width="10"
        stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${offset}"
        transform="rotate(-90 60 60)"/>
      <text x="60" y="66" text-anchor="middle" font-size="26" font-weight="800" fill="var(--ink)">${num}</text>
    </svg>`;
}

async function submitRedeem() {
  const input = document.getElementById('redeemInput');
  const msg = document.getElementById('redeemMsg');
  const code = input.value.trim();
  if (!code) return;
  try {
    const res = await api('/redeem', 'POST', { code });
    msg.style.color = 'var(--sage)';
    msg.textContent = res.message;
    input.value = '';
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
    loadProfile();
  } catch (e) {
    msg.style.color = 'var(--rose)';
    msg.textContent = e.message;
  }
}

/* ---------------- Boot ---------------- */
async function boot() {
  setTopDate();
  try {
    const auth = await authenticate();
    window.PROFILE_NAME = auth.full_name;
    document.getElementById('loading').style.display = 'none';
    document.getElementById('screen-review').classList.add('active');
    await loadReview();
  } catch (e) {
    document.getElementById('loading').textContent = 'خطا در ورود: ' + e.message;
  }
}
boot();
</script>
</body>
</html>
"""

