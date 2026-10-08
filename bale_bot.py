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

BOT_TOKEN = os.environ["BALE_BOT_TOKEN"]
BALE_API_BASE = "https://tapi.bale.ai/bot"
BALE_FILE_BASE = "https://tapi.bale.ai/file/bot"
API_BASE_URL = os.environ.get("API_BASE_URL", f"http://127.0.0.1:{os.environ.get('PORT', '8000')}")
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

# --- جزوه ---
# chat_id -> آخرین خلاصه‌ای که نشون دادیم و کاربر می‌تونه تو جزوه ذخیره‌اش کنه
_pending_jozve_summary: dict[int, dict] = {}
# chat_id -> "save" (بعد از ساخت درس، خلاصه‌ی منتظر هم ذخیره بشه) یا "create" (فقط ساخت درس)
_pending_course_name: dict[int, str] = {}
# chat_id -> item_id ای که منتظر شماره‌ی جدید هستیم
_pending_item_number: dict[int, str] = {}
# chat_id -> item_id ای که قراره به درس دیگه منتقل بشه
_pending_item_move: dict[int, str] = {}

# --- جزوه‌ی کامل ---
# chat_id -> note_id ای که کاربر منتظر انتخاب درس برای ساخت جزوه‌ی کاملشه
_pending_full_notes: dict[int, str] = {}
# chat_id -> True اگه کاربر صریحاً اجازه داده هوش مصنوعی جاهای نامفهوم رو حدس بزنه
_pending_guess: dict[int, bool] = {}
# note_id -> {"chars", "used"}: خلاصه از چند حرفِ متن ساخته شد (برای هشدار «خلاصه کامل نیست»)
_summary_meta: dict[str, dict] = {}
# نگه‌داشتن رفرنس تسک‌های پیگیریِ جزوه‌ی کامل تا garbage collect نشن
_follow_tasks: set = set()

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


def _skips_rate_limit(update: Update) -> bool:
    """منوی جزوه هیچ مصرف AI ای نداره، پس مشمول سقف تعداد درخواست نیست (به‌جز ساخت فایل خروجی)."""
    query = update.callback_query
    if query and query.data:
        if query.data.startswith(("fnall:", "fnnew:", "fnx:", "fng:", "fngy:")):  # فقط منوی انتخاب درس؛ ساخت کار (fnc:) محدود می‌مونه
            return True
        return query.data.startswith("jz") and not query.data.startswith(("jzx:", "jzxi:"))
    message = update.message
    if message and message.text:
        chat_id = update.effective_chat.id if update.effective_chat else None
        return (
            message.text == BTN_JOZVE
            or chat_id in _pending_course_name
            or chat_id in _pending_item_number
        )
    return False


def rate_limited(handler):
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id is not None and not _skips_rate_limit(update):
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
BTN_JOZVE = "📒 جزوه‌هام"
BTN_WEB = "🌐 وب‌اپ"
# آدرس عمومی سرویس (برای ساخت لینک وب‌اپ)؛ تو Render می‌تونی با WEB_APP_BASE_URL عوضش کنی
WEB_APP_BASE_URL = os.environ.get("WEB_APP_BASE_URL", "https://unimate-ai-11zr.onrender.com")

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_SEARCH], [BTN_MY_NOTES, BTN_JOZVE], [BTN_HELP, BTN_CREDITS], [BTN_REDEEM, BTN_REVIEW], [BTN_INVITE, BTN_WEB]],
    resize_keyboard=True,
)

# متن دکمه‌های منوی اصلی؛ اگه کاربر وسط یه مرحله‌ی «منتظر متن» یکی‌شون رو بزنه، اون مرحله لغو می‌شه
_MAIN_BUTTON_TEXTS = {BTN_SEARCH, BTN_MY_NOTES, BTN_JOZVE, BTN_WEB, BTN_HELP, BTN_CREDITS, BTN_REDEEM, BTN_REVIEW, BTN_INVITE}

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
                "telegram_user_id": f"bale-{telegram_user_id}",
                "bot_secret": BOT_SHARED_SECRET,
            }
            if referred_by:
                payload["referred_by"] = f"bale-{referred_by}"
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
            InlineKeyboardButton("📖 جزوه‌ی کامل", callback_data=f"fn:{note_id}"),
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
    f"با «{BTN_JOZVE}» خلاصه‌هات رو بر اساس درس دسته‌بندی و شماره‌گذاری می‌کنی و هر وقت خواستی از هر درس "
    "یه جزوه‌ی مرتب (PDF یا DOCX) می‌گیری. زیر هر خلاصه دکمه‌ی «ذخیره در جزوه» هست. "
    "با «⭐ درس فعال» ویس‌ها و فایل‌های صوتی کلاس خودکار خلاصه و تو همون درس ذخیره می‌شن.\n"
    "خلاصه ممکنه بعضی مطالب رو حذف کنه؛ با «📖 جزوه‌ی کامل» از کل متن یه جزوه‌ی مرتب و بدون حذف مطلب می‌گیری "
    "(هزینه‌ش به طول متن بستگی داره و قبل از شروع بهت نشون داده می‌شه).\n\n"
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

    # اگه درس فعال داریم، اسمش به Whisper کمک می‌کنه اصطلاح‌های اون درس رو درست بشنوه؛ هر خطایی نادیده گرفته می‌شه
    upload_params = {}
    if filename.lower().endswith(AUDIO_EXTS):
        try:
            active = (await api_request(user_id, "GET", "/jozve/active", timeout=15)).json().get("course")
            if active:
                upload_params["hint"] = active["name"]
        except Exception:
            pass

    with tempfile.NamedTemporaryFile(delete=False, suffix="_" + filename) as tmp:
        await tg_file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as f:
            resp = await api_request(
                user_id, "POST", "/notes/upload", files={"file": (filename, f)}, params=upload_params, timeout=900
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

    quality = result.get("transcript_quality")
    if quality in ("poor", "fair"):
        await message.reply_text(
            _quality_notice(quality),
            reply_markup=(
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔮 اجازه‌ی حدس هوش مصنوعی + جزوه‌ی کامل", callback_data=f"fng:{note_id}")]]
                )
                if quality == "poor"
                else None
            ),
        )

    # کیفیت بد = متن غلط؛ خلاصه‌ی خودکار اعتبار رو هدر می‌ده، پس فقط دستی
    if quality == "poor":
        return
    await _auto_save_to_active_course(
        message, user_id, note_id, filename, result.get("processing_status")
    )


async def _fetch_action_text(user_id: int, action: str, note_id: str) -> str:
    if action == "text":
        resp = await api_request(user_id, "GET", f"/notes/{note_id}")
        return resp.json().get("extracted_text") or "متنی استخراج نشده."

    if action == "summary":
        resp = await api_request(user_id, "POST", f"/ai/notes/{note_id}/summarize")
        data = resp.json()
        if data.get("text_chars"):
            _summary_meta[note_id] = {"chars": data["text_chars"], "used": data.get("used_chars", data["text_chars"])}
        return data.get("summary", "") or "خلاصه‌ای ساخته نشد."

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
    if action == "summary":
        await _offer_jozve_save(update.message, chat_id, note_id, text)


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

    if query.data.startswith("fn"):
        await _handle_full_notes_button(query, user_id)
        return

    if query.data.startswith("jz"):
        await _handle_jozve_button(query, user_id)
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
    if action == "summary":
        await _offer_jozve_save(query.message, query.message.chat_id, note_id, text)


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


# ===================== جزوه (دسته‌بندی خلاصه‌ها بر اساس درس) =====================
JOZVE_PAGE_SIZE = 10
FULL_NOTES_POLL_SECONDS = 6
FULL_NOTES_MAX_WAIT_SECONDS = 30 * 60
AUDIO_EXTS = (".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".aac", ".flac", ".amr", ".wma")
_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _jz_error(e: httpx.HTTPStatusError) -> str:
    """پیام‌های فارسیِ خودِ router جزوه (۴۰۰/۴۰۴) رو مستقیم نشون می‌ده؛ بقیه‌ی خطاها مثل قبل."""
    if e.response.status_code in (400, 404, 409):
        try:
            detail = e.response.json().get("detail")
        except ValueError:
            detail = None
        if isinstance(detail, str):
            return detail
    return _error_message(e)


def _jz_short(text: str, n: int = 40) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _jz_courses_keyboard(courses: list, prefix: str, new_cb: str | None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{'⭐' if c.get('is_active') else '📘'} {_jz_short(c['name'], 30)} ({c['items_count']})",
                callback_data=f"{prefix}:{c['id']}",
            )
        ]
        for c in courses
    ]
    if new_cb:
        rows.append([InlineKeyboardButton("➕ درس جدید", callback_data=new_cb)])
    return InlineKeyboardMarkup(rows)


async def _jz_nav(query, text: str, reply_markup=None) -> None:
    """منوها رو روی همون پیام ویرایش می‌کنه تا چت شلوغ نشه؛ اگه نشد، پیام جدید می‌فرسته."""
    try:
        await query.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await query.message.reply_text(text, reply_markup=reply_markup)


async def _offer_jozve_save(message, chat_id: int, note_id: str, text: str) -> None:
    if not text or text.startswith("خلاصه‌ای ساخته نشد"):
        return
    _pending_jozve_summary[chat_id] = {"note_id": note_id, "text": text}
    notice = _truncation_notice(note_id)
    await message.reply_text(
        (notice + "\n\n" if notice else "") + "می‌خوای این خلاصه تو جزوه‌ات (دسته‌بندی‌شده بر اساس درس) ذخیره بشه؟",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💾 ذخیره در جزوه", callback_data=f"jzsave:{note_id}")],
                [InlineKeyboardButton("📖 جزوه‌ی کامل (بدون حذف مطلب)", callback_data=f"fn:{note_id}")],
            ]
        ),
    )


def _quality_notice(quality: str) -> str:
    if quality == "poor":
        return (
            "⚠️ کیفیت تبدیل صدا به متن خیلی پایین بود؛ متن احتمالاً پر از کلمه‌ی غلطه و خلاصه یا جزوه‌ی ساخته‌شده ازش هم غلط می‌شه.\n"
            "بخش‌هایی که سیستم بهشون اطمینان نداشته تو متن با [؟ ...] علامت خورده‌ن و این علامت تو خلاصه، جزوه‌ی کامل و خروجی هم می‌مونه.\n"
            "قبل از هر کاری «📄 متن» رو ببین. اگه می‌خوای به هر حال ادامه بدی، از دکمه‌های پیام بالا استفاده کن؛ "
            "به همین خاطر خلاصه‌ی خودکار انجام نشد.\n"
            "اگه دوست داری هوش مصنوعی جاهای نامفهوم رو حدس بزنه، دکمه‌ی زیر رو بزن (فقط با اجازه‌ی تو و با علامت).\n"
            "برای نتیجه‌ی بهتر، ضبط رو نزدیک‌تر به استاد و بدون نویز انجام بده و فایل اصلی ضبط رو بفرست، نه ویس فورواردشده."
        )
    return (
        "⚠️ کیفیت صدا متوسط بود؛ ممکنه بعضی کلمه‌ها غلط باشن. بخش‌های مشکوک تو متن با [؟ ...] علامت خورده‌ن؛ "
        "قبل از استفاده «📄 متن» رو یه نگاه بنداز."
    )


def _truncation_notice(note_id: str) -> str:
    """اگه خلاصه فقط از بخشی از متن ساخته شده باشه، به کاربر می‌گه."""
    meta = _summary_meta.get(note_id)
    if not meta or meta["chars"] <= meta["used"]:
        return ""
    percent = max(1, round(meta["used"] / meta["chars"] * 100))
    return (
        f"⚠️ این خلاصه فقط از حدود {percent}٪ ابتدای متن ساخته شده (متن حدود {meta['chars'] // 1000} هزار حرفه) "
        "و بقیه‌ی مطلب توش نیست. برای پوشش کامل، «📖 جزوه‌ی کامل» رو بزن."
    )


async def _course_name(user_id: int, course_id: str) -> str:
    courses = (await api_request(user_id, "GET", "/jozve/courses")).json()
    return next((c["name"] for c in courses if c["id"] == course_id), "درس")


async def _save_summary_to_course(user_id: int, chat_id: int, course_id: str) -> dict | None:
    pending = _pending_jozve_summary.get(chat_id)
    if not pending:
        return None
    resp = await api_request(
        user_id, "POST", "/jozve/items",
        json={"course_id": course_id, "content": pending["text"], "note_id": pending["note_id"]},
    )
    _pending_jozve_summary.pop(chat_id, None)  # جلوگیری از ذخیره‌ی تکراریِ همون خلاصه
    return resp.json()


def _activate_offer(data: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⭐ ویس‌های بعدی هم خودکار اینجا ذخیره بشن", callback_data=f"jzact:{data['course_id']}")]]
    )


def _saved_message(data: dict) -> str:
    return (
        f"✅ تو «{data['course_name']}» ذخیره شد — شماره {data['number']}\n"
        f"برای تغییر شماره، انتقال یا گرفتن جزوه: «{BTN_JOZVE}»"
    )


async def _show_courses(send, user_id: int) -> None:
    courses = (await api_request(user_id, "GET", "/jozve/courses")).json()
    if not courses:
        await send(
            "هنوز درسی نساختی. وقتی یه خلاصه رو «ذخیره در جزوه» کنی همون موقع می‌تونی درس بسازی؛ "
            "یا از دکمه‌ی زیر همین الان یکی بساز.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("➕ درس جدید", callback_data="jznew:create")]]
            ),
        )
        return
    active = next((c for c in courses if c.get("is_active")), None)
    header = "📒 جزوه‌هام — یه درس رو انتخاب کن:"
    if active:
        header = f"⭐ درس فعال: «{active['name']}» (ویس‌ها و فایل‌های صوتی خودکار اینجا ذخیره می‌شن)\n\n" + header
    await send(
        header,
        reply_markup=_jz_courses_keyboard(courses, "jzo", "jznew:create"),
    )


async def _show_course(send, user_id: int, course_id: str) -> None:
    data = (await api_request(user_id, "GET", f"/jozve/courses/{course_id}/items")).json()
    items = data["items"]
    is_active = bool(data["course"].get("is_active"))
    lines = [f"{'⭐' if is_active else '📘'} {data['course']['name']}", f"{len(items)} خلاصه"]
    if is_active:
        lines.append("⭐ درس فعال: فایل‌های صوتی جدید خودکار اینجا ذخیره می‌شن")
    lines.append("")
    for it in items[:40]:
        badge = "📖 " if it.get("kind") == "full" else ""
        lines.append(f"{badge}{it['number']}. {_jz_short(it.get('title') or 'بدون عنوان')}")
    if len(items) > 40:
        lines.append(f"… و {len(items) - 40} خلاصه‌ی دیگه")
    rows = []
    if items:
        rows.append(
            [
                InlineKeyboardButton("📄 PDF", callback_data=f"jzx:pdf:{course_id}"),
                InlineKeyboardButton("📝 DOCX", callback_data=f"jzx:docx:{course_id}"),
            ]
        )
        rows.append([InlineKeyboardButton("✏️ مدیریت خلاصه‌ها", callback_data=f"jzm:{course_id}:0")])
    if is_active:
        rows.append([InlineKeyboardButton("⏹ خاموش کردن درس فعال", callback_data="jzoff:0")])
    else:
        rows.append([InlineKeyboardButton("⭐ درس فعال کن", callback_data=f"jzact:{course_id}")])
    rows.append([InlineKeyboardButton("🗑 حذف درس", callback_data=f"jzcd:{course_id}")])
    rows.append([InlineKeyboardButton("⬅️ همه‌ی درس‌ها", callback_data="jzback:0")])
    await send("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def _show_manage(send, user_id: int, course_id: str, page: int) -> None:
    data = (await api_request(user_id, "GET", f"/jozve/courses/{course_id}/items")).json()
    items = data["items"]
    pages = max(1, -(-len(items) // JOZVE_PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    chunk = items[page * JOZVE_PAGE_SIZE : (page + 1) * JOZVE_PAGE_SIZE]
    rows = [
        [
            InlineKeyboardButton(
                f"{'📖 ' if it.get('kind') == 'full' else ''}{it['number']}. {_jz_short(it.get('title') or 'بدون عنوان', 33)}",
                callback_data=f"jzi:{it['id']}",
            )
        ]
        for it in chunk
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"jzm:{course_id}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"jzm:{course_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ برگشت به درس", callback_data=f"jzo:{course_id}")])
    title = f"✏️ {data['course']['name']}\nیه خلاصه رو برای دیدن، تغییر شماره، انتقال یا حذف انتخاب کن"
    if pages > 1:
        title += f" (صفحه {page + 1} از {pages})"
    await send(title, reply_markup=InlineKeyboardMarkup(rows))


async def _show_item(query, user_id: int, item_id: str) -> None:
    item = (await api_request(user_id, "GET", f"/jozve/items/{item_id}")).json()
    head = f"📌 شماره {item['number']} — {item.get('title') or 'بدون عنوان'}"
    await _send_long(query.message.reply_text, f"{head}\n\n{item['content']}")
    await query.message.reply_text(
        "چیکار کنم؟",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔢 تغییر شماره", callback_data=f"jzn:{item_id}"),
                    InlineKeyboardButton("📂 انتقال به درس دیگه", callback_data=f"jzmv:{item_id}"),
                ],
                [
                    InlineKeyboardButton("📄 PDF", callback_data=f"jzxi:pdf:{item_id}"),
                    InlineKeyboardButton("📝 DOCX", callback_data=f"jzxi:docx:{item_id}"),
                ],
                [InlineKeyboardButton("🗑 حذف", callback_data=f"jzd:{item_id}")],
                [InlineKeyboardButton("⬅️ برگشت", callback_data=f"jzm:{item['course_id']}:0")],
            ]
        ),
    )


async def _export_course(query, user_id: int, fmt: str, course_id: str) -> None:
    if fmt not in ("pdf", "docx"):
        return
    await query.message.reply_text("در حال ساخت فایل...")
    name = await _course_name(user_id, course_id)
    resp = await api_request(
        user_id, "GET", f"/jozve/courses/{course_id}/export", params={"format": fmt}, timeout=120
    )
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")[:50] or "jozve"
    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}") as tmp:
        tmp.write(resp.content)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            await query.message.reply_document(f, filename=f"{safe}.{fmt}")
    finally:
        os.unlink(tmp_path)


# ===================== جزوه‌ی کامل (ساخته‌شده از کل متن، بدون خلاصه‌سازی) =====================
def _fn_error(e: httpx.HTTPStatusError) -> str:
    if e.response.status_code in (400, 404, 409):
        return _jz_error(e)
    return _error_message(e)


async def _fn_menu(query, user_id: int, chat_id: int, note_id: str, guess: bool = False) -> None:
    """هزینه و زمان رو نشون می‌ده و می‌پرسه تو کدوم درس ذخیره بشه."""
    est = (await api_request(user_id, "GET", f"/jozve/full-notes/estimate/{note_id}")).json()
    courses = (await api_request(user_id, "GET", "/jozve/courses")).json()
    _pending_full_notes[chat_id] = note_id
    if guess:
        _pending_guess[chat_id] = True
    else:
        _pending_guess.pop(chat_id, None)

    lines = [
        "📖 جزوه‌ی کامل" + (" — با حدس هوش مصنوعی 🔮" if guess else ""),
        "برخلاف خلاصه، هیچ مطلبی حذف نمی‌شه؛ فقط حرف‌های اضافه و تکرار برداشته می‌شن و متن مرتب و تیتربندی می‌شه.",
        "",
        f"متن: حدود {max(1, est['used_chars'] // 1000)} هزار حرف ← {est['parts']} بخش",
        f"هزینه: {est['cost']} اعتبار",
        f"زمان تقریبی: حدود {est['minutes']} دقیقه (می‌تونی تو این مدت از ربات استفاده کنی)",
    ]
    if est.get("truncated"):
        lines.append("⚠️ متن خیلی بلنده؛ فقط ۱۵۰ هزار حرف اولش پردازش می‌شه.")
    if est.get("quality") in ("poor", "fair"):
        lines.append("")
        lines.append(_quality_notice(est["quality"]).split("\n")[0])
        if est["quality"] == "poor":
            lines.append("جزوه‌ی کامل وفادار به متنه، پس از این متن هم جزوه‌ی غلط می‌سازه. پیشنهاد می‌کنم اول متن رو چک کنی.")

    if guess:
        lines.append("\n🔮 اجازه‌ی حدس داده شده: جاهای نامفهوم با [حدس: ...] مشخص می‌شن و قطعی نیستن.")
    active = next((c for c in courses if c.get("is_active")), None)
    rows = []
    if active:
        lines.append(f"\nتو «{active['name']}» (درس فعال) ذخیره بشه؟")
        rows.append([InlineKeyboardButton(f"✅ شروع — {_jz_short(active['name'], 25)}", callback_data=f"fnc:{active['id']}")])
        rows.append([InlineKeyboardButton("📂 تو درس دیگه", callback_data="fnall:0")])
    else:
        lines.append("\nتو کدوم درس ذخیره بشه؟")
        rows = _jz_courses_keyboard(courses, "fnc", "fnnew:0").inline_keyboard
        rows = [list(r) for r in rows]
    if est.get("quality") == "poor" and not guess:
        rows.append([InlineKeyboardButton("🔮 با حدس هوش مصنوعی (نیاز به اجازه‌ی تو)", callback_data=f"fng:{note_id}")])
    rows.append([InlineKeyboardButton("انصراف", callback_data="fnx:0")])
    await query.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def _start_full_notes(message, user_id: int, chat_id: int, course_id: str) -> None:
    note_id = _pending_full_notes.pop(chat_id, None)
    guess = _pending_guess.pop(chat_id, False)
    if not note_id:
        await message.reply_text("این درخواست منقضی شده؛ دوباره «📖 جزوه‌ی کامل» رو بزن.")
        return
    body = {"note_id": note_id, "course_id": course_id}
    if guess:
        body["guess"] = True
    try:
        job = (await api_request(user_id, "POST", "/jozve/full-notes", json=body)).json()
    except httpx.HTTPStatusError as e:
        await message.reply_text(_fn_error(e))
        return
    task = asyncio.create_task(_follow_full_notes(message, user_id, job))
    _follow_tasks.add(task)
    task.add_done_callback(_follow_tasks.discard)


async def _follow_full_notes(message, user_id: int, job: dict) -> None:
    """پیشرفت کار رو پیگیری می‌کنه و وقتی تموم شد خبر می‌ده. هندلرهای بقیه‌ی کاربرها رو قفل نمی‌کنه."""
    total = job["parts"]
    progress_msg = None
    try:
        progress_msg = await message.reply_text(
            f"⏳ ساخت جزوه‌ی کامل شروع شد ({total} بخش، هزینه {job['cost']} اعتبار، "
            f"حدود {job['minutes']} دقیقه).\nوقتی آماده شد خبرت می‌کنم."
        )
    except Exception:
        logger.exception("Full-notes start message failed")

    started = time.time()
    last_shown = -1
    last_edit = 0.0
    errors = 0
    while True:
        await asyncio.sleep(FULL_NOTES_POLL_SECONDS)
        if time.time() - started > FULL_NOTES_MAX_WAIT_SECONDS:
            await message.reply_text(
                "ساخت جزوه‌ی کامل از حد انتظار طولانی‌تر شد. چند دقیقه‌ی دیگه «📒 جزوه‌هام» رو چک کن؛ "
                "اگه نبود، دوباره امتحان کن."
            )
            return
        try:
            st = (await api_request(user_id, "GET", f"/jozve/full-notes/{job['job_id']}", timeout=30)).json()
        except httpx.HTTPStatusError as e:
            await message.reply_text(_fn_error(e))
            return
        except Exception:
            errors += 1
            if errors >= 10:
                await message.reply_text("ارتباط با سرور قطع شد. چند دقیقه‌ی دیگه «📒 جزوه‌هام» رو چک کن.")
                return
            continue
        errors = 0

        if st["status"] == "running":
            done = st.get("done_parts") or 0
            if progress_msg and done != last_shown and time.time() - last_edit >= 10:
                try:
                    await progress_msg.edit_text(f"⏳ جزوه‌ی کامل در حال ساخت: بخش {done} از {st.get('total_parts') or total}...")
                    last_shown, last_edit = done, time.time()
                except Exception:
                    pass
            continue

        if st["status"] == "done":
            text = (
                f"✅ جزوه‌ی کامل آماده شد و تو «{st['course_name']}» ذخیره شد — شماره {st['number']}\n"
                f"{total} بخش، حدود {max(1, (st.get('chars') or 0) // 1000)} هزار حرف"
            )
            if st.get("guess"):
                text += "\n🔮 جاهای نامفهوم با [حدس: ...] مشخص شدن؛ قطعی نیستن و باید چک بشن."
            if st.get("failed_parts"):
                text += (
                    f"\n⚠️ {st['failed_parts']} بخش پردازش نشد و به‌صورت متن خام (با علامت ⚠️) تو جزوه اومده؛ "
                    "هیچ مطلبی از دست نرفته."
                )
            await message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("📄 PDF", callback_data=f"jzxi:pdf:{st['item_id']}"),
                            InlineKeyboardButton("📝 DOCX", callback_data=f"jzxi:docx:{st['item_id']}"),
                        ],
                        [InlineKeyboardButton("👁 مشاهده تو چت", callback_data=f"jzi:{st['item_id']}")],
                        [InlineKeyboardButton("📘 باز کردن درس", callback_data=f"jzo:{st['course_id']}")],
                    ]
                ),
            )
            return

        await message.reply_text("❌ " + (st.get("error") or "ساخت جزوه‌ی کامل ناموفق بود."))
        return


async def _handle_full_notes_button(query, user_id: int) -> None:
    chat_id = query.message.chat_id
    action, _, rest = query.data.partition(":")
    try:
        if action == "fn":
            await _fn_menu(query, user_id, chat_id, rest)

        elif action == "fng":
            await query.message.reply_text(
                "🔮 حدس هوش مصنوعی\n"
                "کیفیت این ضبط خیلی پایینه. اگه اجازه بدی، هوش مصنوعی برای جاهای نامفهوم حدس می‌زنه استاد چی گفته.\n\n"
                "⚠️ حدس‌ها داخل [حدس: ...] میان و قطعی نیستن؛ ممکنه کاملاً غلط باشن. بدون چک‌کردن با استاد یا منبع، برای حفظ‌کردن ازشون استفاده نکن.\n"
                "از خودش عدد، تاریخ یا اسم نمی‌سازه و بقیه‌ی متن دست‌نخورده می‌مونه.\n\n"
                "اجازه می‌دی؟",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("✅ اجازه می‌دم، حدس بزن", callback_data=f"fngy:{rest}")],
                        [InlineKeyboardButton("❌ نه، بدون حدس", callback_data=f"fn:{rest}")],
                    ]
                ),
            )

        elif action == "fngy":
            await _fn_menu(query, user_id, chat_id, rest, guess=True)

        elif action == "fnall":
            courses = (await api_request(user_id, "GET", "/jozve/courses")).json()
            await query.message.reply_text(
                "تو کدوم درس ذخیره بشه؟",
                reply_markup=_jz_courses_keyboard(courses, "fnc", "fnnew:0"),
            )

        elif action == "fnc":
            await _start_full_notes(query.message, user_id, chat_id, rest)

        elif action == "fnnew":
            if chat_id not in _pending_full_notes:
                await query.message.reply_text("این درخواست منقضی شده؛ دوباره «📖 جزوه‌ی کامل» رو بزن.")
                return
            _pending_course_name[chat_id] = "full"
            await query.message.reply_text("اسم درس رو بفرست (یا بنویس «لغو»):")

        elif action == "fnx":
            _pending_full_notes.pop(chat_id, None)
            await query.message.reply_text("لغو شد.")

    except httpx.HTTPStatusError as e:
        await query.message.reply_text(_fn_error(e))
    except Exception:
        logger.exception("Full-notes button failed: %s", query.data)
        await query.message.reply_text("یه خطای غیرمنتظره پیش اومد.")


async def _export_item(query, user_id: int, fmt: str, item_id: str) -> None:
    if fmt not in ("pdf", "docx"):
        return
    await query.message.reply_text("در حال ساخت فایل...")
    resp = await api_request(
        user_id, "GET", f"/jozve/items/{item_id}/export", params={"format": fmt}, timeout=120
    )
    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}") as tmp:
        tmp.write(resp.content)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            await query.message.reply_document(f, filename=f"jozve.{fmt}")
    finally:
        os.unlink(tmp_path)


async def _auto_save_to_active_course(message, user_id: int, note_id: str, filename: str, status) -> None:
    """اگه درس فعال تنظیم شده باشه، فایل صوتیِ تازه‌آپلودشده رو خودکار خلاصه می‌کنه و تو همون درس ذخیره می‌کنه.
    هر خطایی اینجا فقط یه پیام می‌ده و جریان عادیِ آپلود رو خراب نمی‌کنه."""
    if status != "done" or not filename.lower().endswith(AUDIO_EXTS):
        return
    try:
        active = (await api_request(user_id, "GET", "/jozve/active")).json().get("course")
    except Exception:
        logger.exception("Active course lookup failed")
        return
    if not active:
        return

    await message.reply_text(f"⭐ درس فعال: «{active['name']}» — در حال خلاصه‌سازی و ذخیره...")
    try:
        text = await _fetch_action_text(user_id, "summary", note_id)
    except httpx.HTTPStatusError as e:
        await message.reply_text(_error_message(e) + "\n(فایلت سر جاشه؛ با دکمه‌های بالا می‌تونی دستی ادامه بدی.)")
        return
    except Exception:
        logger.exception("Auto summary failed")
        await message.reply_text("خلاصه‌سازی خودکار انجام نشد؛ با دکمه‌های بالا می‌تونی دستی ادامه بدی.")
        return
    if not text or text.startswith("خلاصه‌ای ساخته نشد"):
        await message.reply_text("خلاصه ساخته نشد، برای همین چیزی تو جزوه ذخیره نشد.")
        return

    await _send_long(message.reply_text, text)
    try:
        data = (
            await api_request(
                user_id, "POST", "/jozve/items",
                json={"course_id": active["id"], "content": text, "note_id": note_id},
            )
        ).json()
    except httpx.HTTPStatusError as e:
        await message.reply_text(_jz_error(e))
        return
    except Exception:
        logger.exception("Auto jozve save failed")
        await message.reply_text("ذخیره‌ی خودکار انجام نشد؛ می‌تونی با «💾 ذخیره در جزوه» دستی ذخیره‌اش کنی.")
        return
    notice = _truncation_notice(note_id)
    await message.reply_text(
        f"✅ تو «{data['course_name']}» ذخیره شد — شماره {data['number']}" + ("\n\n" + notice if notice else ""),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("↩️ لغو ذخیره", callback_data=f"jzundo:{data['id']}"),
                    InlineKeyboardButton("⏹ خاموش کردن", callback_data="jzoff:0"),
                ],
                [InlineKeyboardButton("📖 ساخت جزوه‌ی کامل از همین فایل", callback_data=f"fn:{note_id}")],
            ]
        ),
    )


async def _handle_jozve_button(query, user_id: int) -> None:
    chat_id = query.message.chat_id
    action, _, rest = query.data.partition(":")
    nav = functools.partial(_jz_nav, query)
    try:
        if action == "jzsave":
            pending = _pending_jozve_summary.get(chat_id)
            if not pending or pending["note_id"] != rest:
                await query.message.reply_text("این خلاصه دیگه در دسترس نیست — دوباره خلاصه‌اش کن.")
                return
            courses = (await api_request(user_id, "GET", "/jozve/courses")).json()
            if not courses:
                _pending_course_name[chat_id] = "save"
                await query.message.reply_text("هنوز درسی نداری. اسم درس رو بفرست (یا بنویس «لغو»):")
                return
            await query.message.reply_text(
                "تو کدوم درس ذخیره بشه؟",
                reply_markup=_jz_courses_keyboard(courses, "jzc", "jznew:save"),
            )

        elif action == "jzc":
            data = await _save_summary_to_course(user_id, chat_id, rest)
            if not data:
                await query.message.reply_text("این خلاصه دیگه در دسترس نیست — دوباره خلاصه‌اش کن.")
                return
            await query.message.reply_text(_saved_message(data), reply_markup=_activate_offer(data))

        elif action == "jznew":
            _pending_course_name[chat_id] = rest if rest in ("save", "create") else "create"
            await query.message.reply_text("اسم درس رو بفرست (یا بنویس «لغو»):")

        elif action == "jzback":
            await _show_courses(nav, user_id)

        elif action == "jzo":
            await _show_course(nav, user_id, rest)

        elif action == "jzm":
            course_id, _, page = rest.partition(":")
            await _show_manage(nav, user_id, course_id, int(page or 0))

        elif action == "jzi":
            await _show_item(query, user_id, rest)

        elif action == "jzn":
            _pending_item_number[chat_id] = rest
            await query.message.reply_text("شماره‌ی جدید رو بفرست (یه عدد صحیح، مثلاً 3 — یا بنویس «لغو»):")

        elif action == "jzmv":
            item = (await api_request(user_id, "GET", f"/jozve/items/{rest}")).json()
            courses = (await api_request(user_id, "GET", "/jozve/courses")).json()
            others = [c for c in courses if c["id"] != item["course_id"]]
            if not others:
                await query.message.reply_text(
                    f"درس دیگه‌ای نداری. اول از «{BTN_JOZVE}» یه درس جدید بساز."
                )
                return
            _pending_item_move[chat_id] = rest
            await query.message.reply_text(
                "به کدوم درس منتقل بشه؟", reply_markup=_jz_courses_keyboard(others, "jzmt", None)
            )

        elif action == "jzmt":
            item_id = _pending_item_move.pop(chat_id, None)
            if not item_id:
                await query.message.reply_text("این درخواست منقضی شده؛ دوباره از «مدیریت خلاصه‌ها» انتقال رو بزن.")
                return
            data = (
                await api_request(user_id, "PATCH", f"/jozve/items/{item_id}", json={"course_id": rest})
            ).json()
            await query.message.reply_text(
                f"✅ منتقل شد (شماره {data['number']} توی درس جدید).",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📘 باز کردن درس", callback_data=f"jzo:{data['course_id']}")]]
                ),
            )

        elif action == "jzd":
            await query.message.reply_text(
                "این خلاصه برای همیشه از جزوه حذف بشه؟",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"jzdy:{rest}"),
                            InlineKeyboardButton("انصراف", callback_data=f"jzi:{rest}"),
                        ]
                    ]
                ),
            )

        elif action == "jzdy":
            await api_request(user_id, "DELETE", f"/jozve/items/{rest}")
            await query.message.reply_text("🗑 حذف شد.")

        elif action == "jzcd":
            name = await _course_name(user_id, rest)
            await nav(
                f"درس «{name}» با همه‌ی خلاصه‌های داخلش حذف بشه؟\n"
                "(خودِ فایل‌ها و نوت‌هات دست‌نخورده می‌مونن.)",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"jzcdy:{rest}"),
                            InlineKeyboardButton("انصراف", callback_data=f"jzo:{rest}"),
                        ]
                    ]
                ),
            )

        elif action == "jzcdy":
            await api_request(user_id, "DELETE", f"/jozve/courses/{rest}")
            await nav(
                "🗑 درس حذف شد.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ همه‌ی درس‌ها", callback_data="jzback:0")]]
                ),
            )

        elif action == "jzact":
            data = (await api_request(user_id, "POST", f"/jozve/courses/{rest}/activate")).json()
            await query.message.reply_text(
                f"⭐ «{data['name']}» درس فعال شد.\n"
                "از این به بعد هر ویس یا فایل صوتی که بفرستی خودکار به متن و خلاصه تبدیل می‌شه و تو این درس با شماره‌ی بعدی ذخیره می‌شه.\n\n"
                "⚠️ هزینه‌ی هر خلاصه همون هزینه‌ی دکمه‌ی «📝 خلاصه» ـه.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⏹ خاموش کردن", callback_data="jzoff:0")]]
                ),
            )

        elif action == "jzoff":
            await api_request(user_id, "DELETE", "/jozve/active")
            await query.message.reply_text("⏹ درس فعال خاموش شد. فایل‌هات مثل قبل فقط پردازش می‌شن.")

        elif action == "jzundo":
            await api_request(user_id, "DELETE", f"/jozve/items/{rest}")
            await query.message.reply_text("↩️ از جزوه برداشته شد (خودِ فایل و نوتت سر جاشه).")

        elif action == "jzxi":
            fmt, _, item_id = rest.partition(":")
            await _export_item(query, user_id, fmt, item_id)

        elif action == "jzx":
            fmt, _, course_id = rest.partition(":")
            await _export_course(query, user_id, fmt, course_id)

    except httpx.HTTPStatusError as e:
        await query.message.reply_text(_jz_error(e))
    except Exception:
        logger.exception("Jozve button failed: %s", query.data)
        await query.message.reply_text("یه خطای غیرمنتظره پیش اومد.")


async def _handle_new_course_name(update: Update, user_id: int, chat_id: int, text: str, mode: str) -> None:
    name = " ".join(text.split())
    if name in ("لغو", "انصراف"):
        await update.message.reply_text("لغو شد.")
        return
    if not name or len(name) > 100:
        _pending_course_name[chat_id] = mode
        await update.message.reply_text("اسم درس باید بین ۱ تا ۱۰۰ حرف باشه. دوباره بفرست (یا بنویس «لغو»):")
        return
    try:
        course = (await api_request(user_id, "POST", "/jozve/courses", json={"name": name})).json()
        if mode == "full":
            await _start_full_notes(update.message, user_id, chat_id, course["id"])
            return
        if mode == "save":
            data = await _save_summary_to_course(user_id, chat_id, course["id"])
            if data:
                await update.message.reply_text(_saved_message(data), reply_markup=_activate_offer(data))
            else:
                await update.message.reply_text(f"درس «{course['name']}» ساخته شد، ولی خلاصه‌ی منتظر ذخیره نبود.")
            return
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(_jz_error(e))
        return
    note = "✅ درس «{}» ساخته شد.".format(course["name"]) if course.get("created") else "این درس از قبل داشتی."
    await update.message.reply_text(
        note,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📘 باز کردن درس", callback_data=f"jzo:{course['id']}")]]
        ),
    )


async def _handle_new_item_number(update: Update, user_id: int, chat_id: int, item_id: str, text: str) -> None:
    raw = text.strip().translate(_FA_DIGITS)
    if raw in ("لغو", "انصراف"):
        await update.message.reply_text("لغو شد.")
        return
    if not raw.isdigit() or not (1 <= int(raw) <= 9999):
        _pending_item_number[chat_id] = item_id
        await update.message.reply_text("یه عدد صحیح بین 1 تا 9999 بفرست (یا بنویس «لغو»):")
        return
    try:
        data = (
            await api_request(user_id, "PATCH", f"/jozve/items/{item_id}", json={"number": int(raw)})
        ).json()
    except httpx.HTTPStatusError as e:
        await update.message.reply_text(_jz_error(e))
        return
    await update.message.reply_text(
        f"✅ شماره‌ی این خلاصه شد {data['number']}.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📘 باز کردن درس", callback_data=f"jzo:{data['course_id']}")]]
        ),
    )


@rate_limited
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text

    if text in _MAIN_BUTTON_TEXTS:
        _pending_course_name.pop(chat_id, None)
        _pending_item_number.pop(chat_id, None)

    if chat_id in _pending_course_name:
        mode = _pending_course_name.pop(chat_id)
        await _handle_new_course_name(update, user_id, chat_id, text, mode)
        return

    if chat_id in _pending_item_number:
        item_id = _pending_item_number.pop(chat_id)
        await _handle_new_item_number(update, user_id, chat_id, item_id, text)
        return

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

    if text == BTN_WEB:
        try:
            code = (await api_request(user_id, "POST", "/panel/link")).json()["code"]
        except httpx.HTTPStatusError as e:
            await update.message.reply_text(_error_message(e))
            return
        url = f"{WEB_APP_BASE_URL.rstrip('/')}/panel?code={code}"
        await update.message.reply_text(
            "🌐 وب‌اپ یونیمیت — فایل‌ها و جزوه‌هات با ربات هماهنگن (هر فایلی که اینجا بفرستی اون‌جا هم هست و برعکس).\n\n"
            "⚠️ وب‌اپ بدون VPN باز نمی‌شه؛ قبل از زدن دکمه، VPN رو روشن کن.\n"
            "لینک ۱۰ دقیقه معتبره و فقط یه‌بار کار می‌کنه؛ بعدش خودش وارد می‌مونی.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌐 باز کردن وب‌اپ", url=url)]]),
        )
        return

    if text == BTN_JOZVE:
        try:
            await _show_courses(update.message.reply_text, user_id)
        except httpx.HTTPStatusError as e:
            await update.message.reply_text(_jz_error(e))
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
            resp = await client.post(
                "/reminders/due", json={"bot_secret": BOT_SHARED_SECRET, "platform": "bale"}
            )
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
            resp = await client.post(
                "/flashcards/nudges/due", json={"bot_secret": BOT_SHARED_SECRET, "platform": "bale"}
            )
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
    # بله دکمه‌ی منوی Web App رو مثل تلگرام پشتیبانی نمی‌کنه (یا فرمتش فرق داره)؛
    # فعلاً کاربر از طریق دکمه‌ی inline که توی /start می‌فرستیم به مینی‌اپ می‌رسه.
    pass


def build_app() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .base_url(BALE_API_BASE)
        .base_file_url(BALE_FILE_BASE)
        .connect_timeout(60)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(60)
        .post_init(_setup_menu_button)
        .build()
    )
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
    return app


def main() -> None:
    """اجرای مستقل (مثلاً تو ترموکس)."""
    logger.info("Bale bot starting (polling)...")
    build_app().run_polling(drop_pending_updates=True)


async def run_bot_async() -> None:
    """اجرا داخل پروسه‌ی API (Render): main.py این رو به‌عنوان تسک پس‌زمینه صدا می‌زنه."""
    application = build_app()
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Bale bot started inside API process (polling)...")


if __name__ == "__main__":
    main()
