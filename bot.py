import asyncio
import functools
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta
from datetime import time as dt_time

import httpx
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")
BOT_SHARED_SECRET = os.environ["BOT_SHARED_SECRET"]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")  # آدرس عمومی HTTPS (مثلاً از Cloudflare Tunnel)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("unimate-bot")

# telegram_user_id -> token. Each Telegram user gets a fully isolated backend
# account; tokens are never shared across users.
_tokens: dict[int, str] = {}
_token_lock = asyncio.Lock()

# chat_id -> last uploaded note_id, so commands/buttons know which note to act on
_last_note_id: dict[int, str] = {}

# chat_id-هایی که منتظر متن جستجو هستن (بعد از زدن /search بدون آرگومان)
_pending_search: set[int] = set()

# chat_id-هایی که منتظر یه کلیدواژه برای جستجو داخل یه نوت خاص هستن
_pending_note_search: dict[int, str] = {}

# chat_id-هایی که منتظر وارد کردن کد ردیم هستن (بعد از زدن دکمه‌ی «فعال‌سازی کد»)
_pending_redeem: set[int] = set()

# chat_id -> note_id ای که منتظر یه عدد دلخواه برای تعداد اسلاید هستن
_pending_slides_count: dict[int, str] = {}

# chat_id -> صف کارت‌های در حال مرور (لیست دیکشنری‌های {id, question, answer})
# و ایندکس فعلی، تا هر بار /review زدن، از همون‌جا که مونده بود ادامه بده
_review_sessions: dict[int, dict] = {}

# chat_id -> آخرین فلش‌کارت‌هایی که ساخته شدن ولی هنوز کاربر تصمیم نگرفته
# (اضافه به مرور یا فقط PDF). تا وقتی کاربر انتخاب نکنه، خودکار ذخیره نمی‌شن.
_pending_flashcards: dict[int, dict] = {}

TELEGRAM_MAX_LEN = 4000

# --- محدودیت تعداد درخواست، برای جلوگیری از سوءاستفاده/مصرف بی‌رویه سهمیه‌ی AI ---
RATE_LIMIT_MAX_ACTIONS = 30
RATE_LIMIT_WINDOW_SECONDS = 10 * 60  # 10 دقیقه
RATE_LIMIT_MIN_INTERVAL = 1.5  # حداقل فاصله بین دو اکشن پشت‌سرهم (ثانیه)

_action_log: dict[int, list[float]] = {}


def _check_rate_limit(user_id: int) -> str | None:
    """None یعنی مجازه. غیر از None یعنی پیام خطاییه که باید نشون داده بشه."""
    now = time.time()  # ساعت واقعی، نه monotonic — چون توی حالت خواب گوشی ممکنه monotonic پیش نره
    timestamps = _action_log.setdefault(user_id, [])

    if timestamps and (now - timestamps[-1]) < RATE_LIMIT_MIN_INTERVAL:
        return "یه‌کم آروم‌تر! چند ثانیه صبر کن و دوباره امتحان کن."

    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    while timestamps and timestamps[0] < cutoff:
        timestamps.pop(0)

    if len(timestamps) >= RATE_LIMIT_MAX_ACTIONS:
        return "به سقف تعداد درخواست در این بازه رسیدی. چند دقیقه دیگه دوباره امتحان کن."

    timestamps.append(now)
    return None


def rate_limited(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id is not None:
            error = _check_rate_limit(user_id)
            if error:
                target = update.message or (update.callback_query and update.callback_query.message)
                if target:
                    await target.reply_text(error)
                if update.callback_query:
                    await update.callback_query.answer()
                return
        return await handler(update, context)

    return wrapper

BTN_SEARCH = "🔎 جستجو در نوت‌ها"
BTN_MY_NOTES = "📚 نوت‌های من"
BTN_HELP = "ℹ️ راهنما"
BTN_CREDITS = "💳 اعتبار من"
BTN_REDEEM = "🎟 فعال‌سازی کد"
BTN_REVIEW = "🔁 مرور فلش‌کارت‌ها"
BTN_OPEN_APP = "🚀 باز کردن اپ"
BTN_INVITE = "🎁 دعوت از دوستان"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_SEARCH], [BTN_MY_NOTES, BTN_HELP], [BTN_CREDITS, BTN_REDEEM], [BTN_REVIEW, BTN_INVITE]],
    resize_keyboard=True,
)

MIN_SLIDES = 3
MAX_SLIDES = 20
SLIDE_COUNT_PRESETS = [5, 10, 15, 20]


async def get_access_token(
    telegram_user_id: int, force_refresh: bool = False, referred_by: str | None = None
) -> str:
    async with _token_lock:
        if telegram_user_id in _tokens and not force_refresh:
            return _tokens[telegram_user_id]
        async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30) as client:
            payload = {
                "telegram_user_id": str(telegram_user_id),
                "bot_secret": BOT_SHARED_SECRET,
            }
            if referred_by:
                payload["referred_by"] = referred_by
            resp = await client.post("/auth/telegram", json=payload)
            resp.raise_for_status()
            token = resp.json()["access_token"]
        _tokens[telegram_user_id] = token
        return token


async def api_request(
    telegram_user_id: int, method: str, path: str, timeout: float = 150, **kwargs
) -> httpx.Response:
    token = await get_access_token(telegram_user_id)
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=timeout) as client:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        resp = await client.request(method, path, headers=headers, **kwargs)
        if resp.status_code == 401:
            token = await get_access_token(telegram_user_id, force_refresh=True)
            headers["Authorization"] = f"Bearer {token}"
            resp = await client.request(method, path, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp


def _require_note(chat_id: int) -> str | None:
    return _last_note_id.get(chat_id)


CONTACT_USERNAME = "@Mzyare"


def _error_message(e: httpx.HTTPStatusError) -> str:
    if e.response.status_code == 402:
        try:
            detail = e.response.json().get("detail")
        except ValueError:
            detail = None
        base = detail if isinstance(detail, str) else "اعتبارت تموم شده."
        return f"{base}\n\nبرای خرید پلن پرمیوم به {CONTACT_USERNAME} پیام بده."
    if e.response.status_code == 403:
        return f"حساب تو توسط مدیر غیرفعال شده. برای اطلاعات بیشتر به {CONTACT_USERNAME} پیام بده."
    if e.response.status_code == 502:
        try:
            detail = e.response.json().get("detail")
        except ValueError:
            detail = None
        if isinstance(detail, str):
            return detail
        return "سرویس هوش مصنوعی موقتاً در دسترس نیست. اعتباری کسر نشد — چند لحظه دیگه دوباره امتحان کن."
    return f"خطا از سمت سرور: {e.response.status_code}"


async def _send_long(send, text: str) -> None:
    if not text:
        text = "(چیزی برنگشت)"
    for i in range(0, len(text), TELEGRAM_MAX_LEN):
        await send(text[i : i + TELEGRAM_MAX_LEN])


def note_keyboard(note_id: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📄 متن", callback_data=f"text:{note_id}"),
            InlineKeyboardButton("📝 خلاصه", callback_data=f"summary:{note_id}"),
        ],
        [
            InlineKeyboardButton("🗂 فلش‌کارت", callback_data=f"flashcards:{note_id}"),
            InlineKeyboardButton("❓ سؤالات", callback_data=f"questions:{note_id}"),
        ],
        [
            InlineKeyboardButton("🌐 ترجمه", callback_data=f"translate:{note_id}"),
            InlineKeyboardButton("🎞 اسلاید", callback_data=f"slides:{note_id}"),
        ],
        [
            InlineKeyboardButton("🔍 جستجو در همین فایل", callback_data=f"searchnote:{note_id}"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def flashcards_result_keyboard(note_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ افزودن به مرور", callback_data=f"flashsave:{note_id}")],
            [InlineKeyboardButton("📄 دریافت PDF رنگی", callback_data=f"flashpdf:{note_id}")],
        ]
    )


HELP_TEXT = (
    "یه فایل PDF، عکس، یا ویس بفرست تا پردازشش کنم؛ یا یه متن معمولی مستقیم "
    "بفرست تا از همون متن نوت بسازم.\n\n"
    "بعد از آپلود، از دکمه‌های زیر پیام استفاده کن (متن، خلاصه، فلش‌کارت، سؤالات، "
    "ترجمه، اسلاید)، یا هر پیام متنی دیگه‌ای بفرستی، به‌عنوان سؤال درباره‌ی همون "
    "فایل ازش می‌پرسم (چت با نوت).\n\n"
    f"روی «اسلاید» که بزنی، می‌تونی تعداد اسلایدها رو انتخاب کنی (بین {MIN_SLIDES} تا "
    f"{MAX_SLIDES} تا)، یا با /slides 12 مستقیم مشخصش کنی.\n\n"
    "فلش‌کارت‌ها الان متناسب با حجم مطلب ساخته می‌شن (مطلب بلندتر → فلش‌کارت "
    "بیشتر). بعد از ساخته شدن، خودت انتخاب می‌کنی که به صف مرور اضافه بشن یا "
    "فقط یه PDF رنگی ازشون بگیری.\n\n"
    f"با «{BTN_SEARCH}» می‌تونی بین همه‌ی نوت‌هات جستجوی معنایی کنی.\n"
    f"با «{BTN_MY_NOTES}» لیست فایل‌هات رو می‌بینی.\n"
    f"با «{BTN_CREDITS}» وضعیت پلن و اعتبارت رو می‌بینی.\n"
    f"با «{BTN_REDEEM}» یه کد شارژ یا پرمیوم رو فعال می‌کنی.\n\n"
    f"با «{BTN_REVIEW}» یا /review فلش‌کارت‌های معوقه رو مرور می‌کنی — توی "
    "مرور، اگه کارتی رو دیگه نمی‌خوای، بدون اینکه مرورش کنی می‌تونی حذفش کنی. "
    "هر روز ساعت ۱۰ صبح اگه کارت معوقه داشته باشی خودم یادت می‌اندازم.\n\n"
    "/studyplan روی یه فایل فعال، یه برنامه‌ی مطالعاتی روزانه می‌سازه.\n"
    "/remind هم یادآوری می‌سازه (مثلاً /remind 2h وقت مطالعه).\n"
    "/credits وضعیت پلن و اعتبار باقی‌مونده‌ت رو نشون می‌ده.\n"
    "/redeem CODE یه کد شارژ یا پرمیوم رو فعال می‌کنه (مثال: /redeem UM-AB12-CD34).\n\n"
    "نوت‌ها و فایل‌های تو کاملاً جدا و خصوصی‌ان — هیچ کاربر دیگه‌ای بهشون دسترسی نداره.\n\n"
    "برای خرید پلن پرمیوم یا هر سؤال دیگه‌ای، به @Mzyare پیام بده."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    referred_by = None
    if context.args and context.args[0].startswith("ref_"):
        candidate = context.args[0][len("ref_"):]
        if candidate.isdigit() and int(candidate) != user_id:
            referred_by = candidate

    # اگه از لینک دعوت اومده، همون اول توکن رو با referred_by می‌گیریم تا اگه
    # کاربر واقعاً تازه‌ست، دعوت روی سرور ثبت بشه (برای کاربرای قدیمی بی‌اثره)
    try:
        await get_access_token(user_id, referred_by=referred_by)
    except Exception:
        logger.exception("Failed to register referral on start")

    await update.message.reply_text(
        "سلام! 👋\n\n" + HELP_TEXT, reply_markup=MAIN_KEYBOARD
    )
    if WEBAPP_URL:
        await update.message.reply_text(
            "برای تجربه‌ی گرافیکی‌تر (مرور فلش‌کارت، نوت‌ها، پروفایل)، از همینجا وارد مینی‌اپ شو:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(BTN_OPEN_APP, web_app=WebAppInfo(url=WEBAPP_URL))]]
            ),
        )


@rate_limited
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    filename = "upload.bin"

    if message.document:
        tg_file = await message.document.get_file()
        filename = message.document.file_name or filename
    elif message.photo:
        tg_file = await message.photo[-1].get_file()
        filename = "photo.jpg"
    elif message.voice:
        tg_file = await message.voice.get_file()
        filename = "voice.ogg"
    elif message.audio:
        tg_file = await message.audio.get_file()
        filename = message.audio.file_name or "audio.mp3"
    else:
        await message.reply_text("فقط فایل (PDF/عکس/ویس) بفرست.")
        return

    await message.reply_text("در حال آپلود و پردازش...")

    with tempfile.NamedTemporaryFile(delete=False, suffix="_" + filename) as tmp:
        await tg_file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as f:
            resp = await api_request(
                user_id, "POST", "/notes/upload", files={"file": (filename, f)}, timeout=600
            )
        result = resp.json()
    except httpx.HTTPStatusError as e:
        logger.exception("Upload failed")
        await message.reply_text(_error_message(e))
        return
    except Exception:
        logger.exception("Upload failed")
        await message.reply_text("یه خطای غیرمنتظره پیش اومد.")
        return
    finally:
        os.unlink(tmp_path)

    note_id = result["id"]
    _last_note_id[chat_id] = note_id

    reply = (
        f"فایل: {result.get('original_filename')}\n"
        f"حجم: {result.get('size_bytes')} بایت\n"
        f"وضعیت پردازش: {result.get('processing_status')}\n\n"
        "می‌تونی از دکمه‌های زیر استفاده کنی یا مستقیم سؤال بپرسی:"
    )
    await message.reply_text(reply, reply_markup=note_keyboard(note_id))


async def _fetch_action_text(user_id: int, action: str, note_id: str) -> str:
    if action == "text":
        resp = await api_request(user_id, "GET", f"/notes/{note_id}")
        return resp.json().get("extracted_text") or "متنی استخراج نشده."

    if action == "summary":
        resp = await api_request(user_id, "POST", f"/ai/notes/{note_id}/summarize")
        return resp.json().get("summary", "") or "خلاصه‌ای ساخته نشد."

    if action == "questions":
        resp = await api_request(user_id, "POST", f"/ai/notes/{note_id}/questions")
        questions = resp.json().get("questions", [])
        if not questions:
            return "سؤالی ساخته نشد."
        lines = []
        for i, q in enumerate(questions, 1):
            opts = "\n".join(f"   {j}) {o}" for j, o in enumerate(q.get("options", [])))
            lines.append(f"{i}. {q.get('question')}\n{opts}\n   پاسخ درست: گزینه {q.get('correct_index')}")
        return "\n\n".join(lines)

    if action == "translate":
        resp = await api_request(user_id, "POST", f"/ai/notes/{note_id}/translate")
        return resp.json().get("translated_text", "") or "ترجمه‌ای ساخته نشد."

    return "دستور نامعتبر."


ACTION_WAIT_MESSAGE = {
    "text": None,
    "summary": "در حال خلاصه‌سازی...",
    "questions": "در حال ساخت سؤالات...",
    "translate": "در حال ترجمه...",
}


async def _run_command_action(action: str, update: Update) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    note_id = _require_note(chat_id)
    if not note_id:
        await update.message.reply_text("اول یه فایل بفرست.")
        return
    wait_msg = ACTION_WAIT_MESSAGE.get(action)
    if wait_msg:
        await update.message.reply_text(wait_msg)
    try:
        text = await _fetch_action_text(user_id, action, note_id)
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(_error_message(e))
        return
    await _send_long(update.message.reply_text, text)


@rate_limited
async def show_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_command_action("text", update)


@rate_limited
async def show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_command_action("summary", update)


async def _generate_flashcards_flow(user_id: int, note_id: str, message) -> None:
    await message.reply_text("در حال ساخت فلش‌کارت... (تعدادش متناسب با حجم مطلبه)")
    try:
        resp = await api_request(user_id, "POST", f"/ai/notes/{note_id}/flashcards", timeout=280)
        cards = resp.json().get("flashcards", [])
    except httpx.HTTPStatusError as e:
        await message.reply_text(_error_message(e))
        return

    if not cards:
        await message.reply_text("فلش‌کارتی ساخته نشد.")
        return

    chat_id = message.chat_id
    _pending_flashcards[chat_id] = {"note_id": note_id, "cards": cards}

    lines = [f"{i}. س: {c.get('question')}\n   ج: {c.get('answer')}" for i, c in enumerate(cards, 1)]
    await _send_long(message.reply_text, "\n\n".join(lines))

    await message.reply_text(
        f"{len(cards)} فلش‌کارت ساخته شد. چیکار کنم؟",
        reply_markup=flashcards_result_keyboard(note_id),
    )


@rate_limited
async def show_flashcards(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    note_id = _require_note(chat_id)
    if not note_id:
        await update.message.reply_text("اول یه فایل بفرست.")
        return
    await _generate_flashcards_flow(user_id, note_id, update.message)


@rate_limited
async def show_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_command_action("questions", update)


@rate_limited
async def show_translate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_command_action("translate", update)


def slides_count_keyboard(note_id: str) -> InlineKeyboardMarkup:
    preset_row = [
        InlineKeyboardButton(str(n), callback_data=f"slidesnum:{n}:{note_id}")
        for n in SLIDE_COUNT_PRESETS
    ]
    return InlineKeyboardMarkup(
        [
            preset_row,
            [InlineKeyboardButton("✏️ عدد دلخواه", callback_data=f"slidescustom:{note_id}")],
        ]
    )


@rate_limited
async def show_slides(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    note_id = _require_note(chat_id)
    if not note_id:
        await update.message.reply_text("اول یه فایل بفرست.")
        return

    if context.args:
        try:
            count = int(context.args[0])
        except ValueError:
            await update.message.reply_text(f"عدد بین {MIN_SLIDES} تا {MAX_SLIDES} بفرست.")
            return
        if not (MIN_SLIDES <= count <= MAX_SLIDES):
            await update.message.reply_text(f"تعداد اسلاید باید بین {MIN_SLIDES} تا {MAX_SLIDES} باشه.")
            return
        await update.message.reply_text("در حال ساخت فایل اسلاید...")
        try:
            await _send_slides(user_id, update.message, note_id, count)
        except httpx.HTTPStatusError as e:
            await update.message.reply_text(_error_message(e))
        return

    await update.message.reply_text(
        "چند اسلاید می‌خوای؟", reply_markup=slides_count_keyboard(note_id)
    )


async def _send_slides(user_id: int, message, note_id: str, slide_count: int) -> None:
    token = await get_access_token(user_id)
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=300) as client:
        resp = await client.post(
            f"/ai/notes/{note_id}/slides",
            headers={"Authorization": f"Bearer {token}"},
            json={"slide_count": slide_count},
        )
        resp.raise_for_status()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pptx") as tmp:
        tmp.write(resp.content)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            await message.reply_document(f, filename="slides.pptx")
    finally:
        os.unlink(tmp_path)


async def _send_flashcards_pdf(user_id: int, message, note_id: str, cards: list) -> None:
    token = await get_access_token(user_id)
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=120) as client:
        resp = await client.post(
            f"/ai/notes/{note_id}/flashcards/pdf",
            headers={"Authorization": f"Bearer {token}"},
            json={"cards": cards},
        )
        resp.raise_for_status()
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(resp.content)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            await message.reply_document(f, filename="flashcards.pdf")
    finally:
        os.unlink(tmp_path)


async def search_in_note(user_id: int, message, note_id: str, keyword: str) -> None:
    keyword = keyword.strip()
    if not keyword:
        await message.reply_text("یه کلیدواژه بفرست.")
        return
    try:
        resp = await api_request(user_id, "GET", f"/notes/{note_id}")
        full_text = resp.json().get("extracted_text") or ""
    except httpx.HTTPStatusError as e:
        await message.reply_text(_error_message(e))
        return

    if not full_text:
        await message.reply_text("این فایل متنی برای جستجو نداره.")
        return

    lower_kw = keyword.lower()
    matches = []
    for line in full_text.splitlines():
        if lower_kw in line.lower():
            matches.append(line.strip())
        if len(matches) >= 15:
            break

    if not matches:
        await message.reply_text(f'چیزی برای "{keyword}" توی این فایل پیدا نشد.')
        return

    header = f'{len(matches)} مورد برای "{keyword}" پیدا شد:\n\n'
    body = "\n\n".join(f"…{m}…" for m in matches)
    await _send_long(message.reply_text, header + body)


@rate_limited
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    await query.answer()

    # این‌ها چند‌بخشی‌ان (بیش از یه ":")، پس قبل از split عمومی جداشون می‌کنیم
    if query.data.startswith("slidesnum:"):
        _, count_str, note_id = query.data.split(":", 2)
        count = int(count_str)
        await query.message.reply_text("در حال ساخت فایل اسلاید...")
        try:
            await _send_slides(user_id, query.message, note_id, count)
        except httpx.HTTPStatusError as e:
            await query.message.reply_text(_error_message(e))
        return

    if query.data.startswith("slidescustom:"):
        _, note_id = query.data.split(":", 1)
        _pending_slides_count[query.message.chat_id] = note_id
        await query.message.reply_text(f"عدد بین {MIN_SLIDES} تا {MAX_SLIDES} بفرست:")
        return

    if query.data.startswith("revshow:"):
        _, card_id = query.data.split(":", 1)
        await _handle_review_show(query, card_id)
        return

    if query.data.startswith("revrate:"):
        _, rating_code, card_id = query.data.split(":", 2)
        await _handle_review_rate(query, user_id, rating_code, card_id)
        return

    if query.data.startswith("revdelete:"):
        _, card_id = query.data.split(":", 1)
        await _handle_review_delete(query, user_id, card_id)
        return

    if query.data.startswith("flashsave:"):
        _, note_id = query.data.split(":", 1)
        await _handle_flashcards_save(query, user_id, note_id)
        return

    if query.data.startswith("flashpdf:"):
        _, note_id = query.data.split(":", 1)
        await _handle_flashcards_pdf(query, user_id, note_id)
        return

    try:
        action, note_id = query.data.split(":", 1)
    except ValueError:
        return

    if action == "searchnote":
        _pending_note_search[query.message.chat_id] = note_id
        await query.message.reply_text("چه کلیدواژه‌ای رو توی این فایل جستجو کنم؟")
        return

    if action == "slides":
        await query.message.reply_text(
            "چند اسلاید می‌خوای؟", reply_markup=slides_count_keyboard(note_id)
        )
        return

    if action == "flashcards":
        await _generate_flashcards_flow(user_id, note_id, query.message)
        return

    wait_msg = ACTION_WAIT_MESSAGE.get(action)
    if wait_msg:
        await query.message.reply_text(wait_msg)

    try:
        text = await _fetch_action_text(user_id, action, note_id)
    except httpx.HTTPStatusError as e:
        await query.message.reply_text(_error_message(e))
        return

    await _send_long(query.message.reply_text, text)


async def _handle_flashcards_save(query, user_id: int, note_id: str) -> None:
    chat_id = query.message.chat_id
    pending = _pending_flashcards.get(chat_id)
    if not pending or pending["note_id"] != note_id:
        await query.message.reply_text("این فلش‌کارت‌ها دیگه در دسترس نیستن — دوباره بسازشون.")
        return
    try:
        resp = await api_request(
            user_id, "POST", "/flashcards/save",
            json={"note_id": note_id, "cards": pending["cards"]},
        )
        saved = resp.json().get("saved_to_review_deck", 0)
    except httpx.HTTPStatusError as e:
        await query.message.reply_text(_error_message(e))
        return
    await query.message.reply_text(f"✅ {saved} فلش‌کارت به صف مرور اضافه شد.")


async def _handle_flashcards_pdf(query, user_id: int, note_id: str) -> None:
    chat_id = query.message.chat_id
    pending = _pending_flashcards.get(chat_id)
    if not pending or pending["note_id"] != note_id:
        await query.message.reply_text("این فلش‌کارت‌ها دیگه در دسترس نیستن — دوباره بسازشون.")
        return
    await query.message.reply_text("در حال ساخت PDF...")
    try:
        await _send_flashcards_pdf(user_id, query.message, note_id, pending["cards"])
    except httpx.HTTPStatusError as e:
        await query.message.reply_text(_error_message(e))


async def search_notes(update: Update, user_id: int, query: str) -> None:
    query = query.strip()
    if not query:
        await update.message.reply_text("یه عبارت برای جستجو بفرست.")
        return
    await update.message.reply_text("در حال جستجو بین نوت‌هات...")
    try:
        resp = await api_request(user_id, "POST", "/notes/search", json={"query": query})
        results = resp.json().get("results", [])
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(_error_message(e))
        return

    if not results:
        await update.message.reply_text("چیزی پیدا نشد. (شاید هنوز نوتی با ایندکس جستجو نداری)")
        return

    lines = []
    for r in results:
        snippet = r["snippet"].replace("\n", " ")
        lines.append(f"📄 {r['filename']} (شباهت: {r['score']})\n{snippet}...")
    await _send_long(update.message.reply_text, "\n\n".join(lines))


async def _show_credits(update: Update, user_id: int) -> None:
    try:
        resp = await api_request(user_id, "GET", "/auth/me")
        data = resp.json()
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(_error_message(e))
        return

    plan_fa = "پرمیوم" if data["plan"] == "premium" else "رایگان"
    text = f"پلن: {plan_fa}"

    if data["plan"] == "premium":
        premium_until = data.get("premium_until")
        if premium_until:
            expires_at = datetime.fromisoformat(premium_until)
            now = datetime.now(expires_at.tzinfo)
            remaining_days = max(0, (expires_at - now).days)
            text += f"\nتا {expires_at.date().isoformat()} فعاله ({remaining_days} روز مونده)"
        else:
            text += "\nپرمیومِ دائمی — بدون تاریخ انقضا"
    else:
        daily_remaining = data.get("daily_remaining", 0)
        daily_total = data.get("daily_total", 10)
        text += f"\nسهمیه‌ی رایگان امروز: {daily_remaining} از {daily_total}"
        text += f"\nاعتبار همیشگی (از دعوت/شارژ): {data['credits']}"
        text += f"\nتعداد دعوت‌های موفق: {data.get('referral_count', 0)}"
        text += f"\n\nبرای دعوت دوستات: /invite"
        text += f"\nبرای خرید پلن پرمیوم: {CONTACT_USERNAME}"

    await update.message.reply_text(text)


@rate_limited
async def cmd_credits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_credits(update, update.effective_user.id)


async def _send_invite_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=ref_{user_id}"

    try:
        resp = await api_request(user_id, "GET", "/auth/me")
        referral_count = resp.json().get("referral_count", 0)
    except httpx.HTTPStatusError:
        referral_count = None

    text = (
        "با این لینک دوستاتو دعوت کن:\n"
        f"{link}\n\n"
        "هر ۳ نفری که با این لینک عضو بشن، ۱۰ کردیت همیشگی می‌گیری."
    )
    if referral_count is not None:
        text += f"\n\nتا الان {referral_count} نفر دعوت کردی."

    await update.message.reply_text(text)


@rate_limited
async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_invite_message(update, context)


async def _do_redeem(update: Update, user_id: int, code: str) -> None:
    try:
        resp = await api_request(user_id, "POST", "/redeem", json={"code": code})
        data = resp.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (400, 404):
            await update.message.reply_text(e.response.json().get("detail", "کد نامعتبره."))
        else:
            await update.message.reply_text(_error_message(e))
        return

    await update.message.reply_text(f"✅ {data['message']}\nپلن فعلی: {data['plan']} — اعتبار: {data['credits']}")


@rate_limited
async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "برای فعال کردن کد، بعد از دستور خودش رو بنویس. مثال:\n/redeem UM-AB12-CD34"
        )
        return

    code = context.args[0].strip()
    await _do_redeem(update, user_id, code)


RATING_CODE = {"a": "again", "h": "hard", "g": "good"}
RATING_LABEL = {"again": "❌ یادم نبود", "hard": "🤔 سخت بود", "good": "✅ بلد بودم"}


def review_reveal_keyboard(card_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🙈 نشون بده جواب", callback_data=f"revshow:{card_id}")],
            [InlineKeyboardButton("🗑 حذف بدون مرور", callback_data=f"revdelete:{card_id}")],
        ]
    )


def review_rate_keyboard(card_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("❌ یادم نبود", callback_data=f"revrate:a:{card_id}"),
                InlineKeyboardButton("🤔 سخت بود", callback_data=f"revrate:h:{card_id}"),
                InlineKeyboardButton("✅ بلد بودم", callback_data=f"revrate:g:{card_id}"),
            ]
        ]
    )


async def _start_review_session(update: Update, user_id: int, chat_id: int) -> None:
    try:
        resp = await api_request(user_id, "GET", "/flashcards/due")
        cards = resp.json()
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(_error_message(e))
        return

    if not cards:
        await update.message.reply_text("چیزی برای مرور نداری — همه‌چی مرورشده‌ست! 🎉")
        return

    _review_sessions[chat_id] = {"cards": cards, "index": 0, "reviewed": 0}
    await update.message.reply_text(f"وقت مروره! {len(cards)} کارت داری.")
    await _show_next_review_card(update.message, chat_id)


async def _show_next_review_card(message, chat_id: int) -> None:
    session = _review_sessions.get(chat_id)
    if not session or session["index"] >= len(session["cards"]):
        reviewed = session["reviewed"] if session else 0
        _review_sessions.pop(chat_id, None)
        await message.reply_text(f"مرورت تموم شد! 👏 {reviewed} کارت مرور کردی. فردا دوباره سر بزن.")
        return

    card = session["cards"][session["index"]]
    await message.reply_text(
        f"❓ {card['question']}", reply_markup=review_reveal_keyboard(card["id"])
    )


async def _handle_review_show(query, card_id: str) -> None:
    chat_id = query.message.chat_id
    session = _review_sessions.get(chat_id)
    if not session:
        await query.message.reply_text("این جلسه‌ی مرور دیگه فعال نیست. یه بار دیگه بزن «🔁 مرور فلش‌کارت‌ها».")
        return

    card = next((c for c in session["cards"] if c["id"] == card_id), None)
    if not card:
        return

    await query.edit_message_text(
        f"❓ {card['question']}\n\n💡 {card['answer']}",
        reply_markup=review_rate_keyboard(card_id),
    )


async def _handle_review_rate(query, user_id: int, rating_code: str, card_id: str) -> None:
    chat_id = query.message.chat_id
    session = _review_sessions.get(chat_id)
    rating = RATING_CODE.get(rating_code, "hard")

    try:
        await api_request(user_id, "POST", f"/flashcards/{card_id}/review", json={"rating": rating})
    except httpx.HTTPStatusError as e:
        await query.message.reply_text(_error_message(e))
        return

    await query.edit_message_text(f"{RATING_LABEL[rating]} ثبت شد.")

    if not session:
        return
    session["index"] += 1
    session["reviewed"] += 1
    await _show_next_review_card(query.message, chat_id)


async def _handle_review_delete(query, user_id: int, card_id: str) -> None:
    chat_id = query.message.chat_id
    try:
        await api_request(user_id, "DELETE", f"/flashcards/{card_id}")
    except httpx.HTTPStatusError as e:
        await query.message.reply_text(_error_message(e))
        return

    await query.edit_message_text("🗑 این کارت بدون مرور حذف شد.")

    session = _review_sessions.get(chat_id)
    if not session:
        return
    session["cards"] = [c for c in session["cards"] if c["id"] != card_id]
    await _show_next_review_card(query.message, chat_id)


@rate_limited
async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_review_session(update, update.effective_user.id, update.effective_chat.id)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    query = " ".join(context.args) if context.args else ""
    if query:
        await search_notes(update, user_id, query)
    else:
        _pending_search.add(chat_id)
        await update.message.reply_text("چی می‌خوای بین نوت‌هات جستجو کنی؟")


@rate_limited
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text

    if chat_id in _pending_study_plan:
        note_id = _pending_study_plan.pop(chat_id)
        await _handle_study_plan_days(update, user_id, note_id, text)
        return

    if chat_id in _pending_note_search:
        note_id = _pending_note_search.pop(chat_id)
        await search_in_note(user_id, update.message, note_id, text)
        return

    if chat_id in _pending_search:
        _pending_search.discard(chat_id)
        await search_notes(update, user_id, text)
        return

    if chat_id in _pending_redeem:
        _pending_redeem.discard(chat_id)
        await _do_redeem(update, user_id, text.strip())
        return

    if chat_id in _pending_slides_count:
        note_id = _pending_slides_count.pop(chat_id)
        try:
            count = int(text.strip())
        except ValueError:
            await update.message.reply_text(f"عدد بین {MIN_SLIDES} تا {MAX_SLIDES} بفرست.")
            return
        if not (MIN_SLIDES <= count <= MAX_SLIDES):
            await update.message.reply_text(f"تعداد اسلاید باید بین {MIN_SLIDES} تا {MAX_SLIDES} باشه.")
            return
        await update.message.reply_text("در حال ساخت فایل اسلاید...")
        try:
            await _send_slides(user_id, update.message, note_id, count)
        except httpx.HTTPStatusError as e:
            await update.message.reply_text(_error_message(e))
        return

    if text == BTN_SEARCH:
        _pending_search.add(chat_id)
        await update.message.reply_text("چی می‌خوای بین نوت‌هات جستجو کنی؟")
        return

    if text == BTN_HELP:
        await update.message.reply_text(HELP_TEXT)
        return

    if text == BTN_CREDITS:
        await _show_credits(update, user_id)
        return

    if text == BTN_REDEEM:
        _pending_redeem.add(chat_id)
        await update.message.reply_text("کد رو بفرست (مثلاً UM-AB12-CD34):")
        return

    if text == BTN_REVIEW:
        await _start_review_session(update, user_id, chat_id)
        return

    if text == BTN_INVITE:
        await _send_invite_message(update, context)
        return

    if text == BTN_MY_NOTES:
        try:
            resp = await api_request(user_id, "GET", "/notes/")
            notes = resp.json()
        except httpx.HTTPStatusError as e:
            await update.message.reply_text(_error_message(e))
            return
        if not notes:
            await update.message.reply_text("هنوز هیچ نوتی نساختی.")
            return
        lines = [
            f"📄 {n['original_filename']} — {n['processing_status']}" for n in notes
        ]
        await _send_long(update.message.reply_text, "\n".join(lines))
        return

    note_id = _require_note(chat_id)

    if note_id:
        # یه فایل فعال هست -> این پیام سؤالیه درباره‌ی همون فایل (چت با نوت)
        try:
            resp = await api_request(
                user_id, "POST", f"/notes/{note_id}/chat", json={"message": text}
            )
            data = resp.json()
        except httpx.HTTPStatusError as e:
            await update.message.reply_text(_error_message(e))
            return
        await _send_long(update.message.reply_text, data.get("content", ""))
        return

    # فایلی فعال نیست -> این متن، محتوای جدیده که باید یه نوت متنی ازش بسازیم
    try:
        resp = await api_request(user_id, "POST", "/notes/from-text", json={"text": text})
        result = resp.json()
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(_error_message(e))
        return

    new_note_id = result["id"]
    _last_note_id[chat_id] = new_note_id
    await update.message.reply_text(
        "متنت ذخیره شد. می‌تونی از دکمه‌های زیر استفاده کنی یا مستقیم سؤال بپرسی:",
        reply_markup=note_keyboard(new_note_id),
    )



STUDY_PLAN_SYSTEM_HINT = "چند روز تا امتحان/deadline داری؟ فقط عدد بفرست (مثلاً 7)."

_pending_study_plan: dict[int, str] = {}


@rate_limited
async def cmd_studyplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    note_id = _require_note(chat_id)
    if not note_id:
        await update.message.reply_text("اول یه فایل بفرست.")
        return
    _pending_study_plan[chat_id] = note_id
    await update.message.reply_text(STUDY_PLAN_SYSTEM_HINT)


async def _handle_study_plan_days(update: Update, user_id: int, note_id: str, text: str) -> None:
    if not text.strip().isdigit():
        await update.message.reply_text("فقط یه عدد بفرست، مثلاً 7")
        return
    days = int(text.strip())
    await update.message.reply_text("در حال ساخت برنامه‌ی مطالعاتی...")
    try:
        resp = await api_request(
            user_id, "POST", f"/ai/notes/{note_id}/study-plan", json={"days": days}
        )
        plan = resp.json().get("plan", [])
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(_error_message(e))
        return

    if not plan:
        await update.message.reply_text("برنامه‌ای ساخته نشد.")
        return

    lines = []
    for day in plan:
        tasks = "\n".join(f"   • {t}" for t in day.get("tasks", []))
        lines.append(f"📅 روز {day.get('day')}: {day.get('focus')}\n{tasks}")
    lines.append(
        "\nبرای یادآوری هر روز می‌تونی بزنی مثلاً:\n/remind 1d وقت مطالعه‌ی روز اول"
    )
    await _send_long(update.message.reply_text, "\n\n".join(lines))


REMIND_RELATIVE_RE = re.compile(r"^(\d+)([mhd])$", re.IGNORECASE)
REMIND_ABSOLUTE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})$")


@rate_limited
async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    args = context.args

    if not args:
        await update.message.reply_text(
            "فرمت درست:\n"
            "/remind 30m متن یادآوری  (۳۰ دقیقه دیگه)\n"
            "/remind 2h متن یادآوری  (۲ ساعت دیگه)\n"
            "/remind 1d متن یادآوری  (۱ روز دیگه)\n"
            "/remind 2026-07-25 18:00 متن یادآوری  (تاریخ دقیق)"
        )
        return

    remind_at = None
    message_start_idx = 1

    rel_match = REMIND_RELATIVE_RE.match(args[0])
    if rel_match:
        amount, unit = int(rel_match.group(1)), rel_match.group(2).lower()
        delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]
        remind_at = datetime.now() + delta
    elif REMIND_ABSOLUTE_RE.match(args[0]) and len(args) >= 2:
        try:
            remind_at = datetime.strptime(f"{args[0]} {args[1]}", "%Y-%m-%d %H:%M")
            message_start_idx = 2
        except ValueError:
            remind_at = None

    if remind_at is None:
        await update.message.reply_text("فرمت زمان درست نیست. /remind رو بدون آرگومان بزن تا راهنما ببینی.")
        return

    message_text = " ".join(args[message_start_idx:]).strip()
    if not message_text:
        await update.message.reply_text("متن یادآوری رو هم بنویس.")
        return

    try:
        await api_request(
            user_id,
            "POST",
            "/reminders",
            json={
                "chat_id": str(chat_id),
                "message": message_text,
                "remind_at": remind_at.isoformat(),
            },
        )
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(_error_message(e))
        return

    await update.message.reply_text(
        f"یادآوری تنظیم شد برای {remind_at.strftime('%Y-%m-%d %H:%M')}."
    )


HEARTBEAT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_heartbeat.txt")


async def check_due_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30) as client:
            resp = await client.post("/reminders/due", json={"bot_secret": BOT_SHARED_SECRET})
            resp.raise_for_status()
            due = resp.json().get("due", [])
    except Exception:
        logger.exception("Failed to check due reminders")
        return

    try:
        with open(HEARTBEAT_PATH, "w") as f:
            f.write(str(time.time()))
    except OSError:
        logger.exception("Failed to write heartbeat file")

    for r in due:
        try:
            await context.bot.send_message(chat_id=int(r["chat_id"]), text=f"⏰ یادآوری: {r['message']}")
        except Exception:
            logger.exception("Failed to send reminder %s", r.get("id"))


async def send_flashcard_nudges(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30) as client:
            resp = await client.post("/flashcards/nudges/due", json={"bot_secret": BOT_SHARED_SECRET})
            resp.raise_for_status()
            targets = resp.json().get("targets", [])
    except Exception:
        logger.exception("Failed to check flashcard nudges")
        return

    for t in targets:
        try:
            await context.bot.send_message(
                chat_id=int(t["chat_id"]),
                text=f"🔔 وقت مرورته! {t['due_count']} کارت برای مرور داری.\nبا «{BTN_REVIEW}» شروع کن.",
            )
        except Exception:
            logger.exception("Failed to send flashcard nudge to %s", t.get("chat_id"))


async def _setup_menu_button(app: Application) -> None:
    try:
        if WEBAPP_URL:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="اپ", web_app=WebAppInfo(url=WEBAPP_URL))
            )
        else:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception:
        logger.exception("Failed to sync menu button")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(_setup_menu_button).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("text", show_text))
    app.add_handler(CommandHandler("summary", show_summary))
    app.add_handler(CommandHandler("flashcards", show_flashcards))
    app.add_handler(CommandHandler("questions", show_questions))
    app.add_handler(CommandHandler("translate", show_translate))
    app.add_handler(CommandHandler("slides", show_slides))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("credits", cmd_credits))
    app.add_handler(CommandHandler("invite", cmd_invite))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CommandHandler("studyplan", cmd_studyplan))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.job_queue.run_repeating(check_due_reminders, interval=30, first=10)
    app.job_queue.run_daily(send_flashcard_nudges, time=dt_time(hour=10, minute=0))
    app.add_handler(
        MessageHandler(
            filters.Document.ALL | filters.PHOTO | filters.VOICE | filters.AUDIO, handle_file
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot starting (polling)...")
    return app


if __name__ == "__main__":
    app = main()
    app.run_polling(drop_pending_updates=True)

def build_app():
    return main()

async def run_bot_async():
    application = main()
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
