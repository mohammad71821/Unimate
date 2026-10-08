"""تبدیل صدا به متن (Groq Whisper) با پیش‌پردازش، تکه‌کردن، فیلتر توهم و امتیاز کیفیت.

مراحل:
1. (اگه ffmpeg باشه) صدا به mono/16kHz تبدیل و بلندی صداش یکدست می‌شه و فشرده می‌شه.
2. فایل‌های بلند سر سکوت‌ها به تکه‌های حدود ۸ دقیقه‌ای بریده می‌شن (حلقه‌ی تکرار و توهم Whisper کمتر می‌شه).
3. هر تکه با زبان فارسی اجباری، پرامپت راهنما و verbose_json تبدیل می‌شه.
4. بخش‌های «توهمِ سکوت» حذف و تکرارهای پشت‌سرهم جمع می‌شن؛ از روی logprob یه امتیاز کیفیت (good/fair/poor) حساب می‌شه.
5. فقط وقتی کیفیت متوسطه، متن تکه‌تکه و با پرامپت محتاط پاک‌سازی می‌شه (کیفیت بد دست‌نخورده می‌مونه تا چیزِ ساختگی تولید نشه).
"""
import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import httpx
from fastapi import HTTPException

from app import fullnotes
from app.ai.gateway import call_ai_safely, get_ai_provider
from app.config import settings

logger = logging.getLogger("unimate.stt")

GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-large-v3"

AUDIO_CONTENT_TYPES = {
    "audio/ogg",
    "audio/opus",
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/mp4",
    "audio/m4a",
    "audio/webm",
}

AUDIO_EXTENSIONS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".webm"}

# --- تنظیمات (برای تنظیم دقیق‌تر فقط همین‌ها رو عوض کن) ---
SEGMENT_TARGET_SECONDS = 480      # طول هدف هر تکه
SEGMENT_MAX_SECONDS = 600         # فایل‌های کوتاه‌تر از این تکه نمی‌شن
SPLIT_SEARCH_WINDOW = 60          # دنبال سکوت تو ±۶۰ ثانیه‌ی نقطه‌ی هدف می‌گردیم
FFMPEG_TIMEOUT = 20 * 60
# کاهش نویز با متغیر محیطی STT_DENOISE تنظیم می‌شه: off (پیش‌فرض) | light | strong
# چون Whisper با صدای نویزی آموزش دیده، حذف شدیدِ نویز گاهی دقت رو کمتر هم می‌کنه؛ برای مقایسه،
# یه فایل رو با دو حالت آپلود کن و مقدار mean_logprob تو لاگ (unimate.stt) رو مقایسه کن.
DENOISE_FILTERS = {
    "off": "",
    "light": "afftdn=nr=10:nf=-30",
    "strong": "afftdn=nr=20:nf=-30:tn=1",
}
RETRY_DELAYS = (0, 5, 15, 30)     # تلاش مجدد برای خطای ۴۲۹/۵xx گروک

# کیفیت: این آستانه‌ها تجربی‌ان؛ مقدارهای واقعی تو لاگ (unimate.stt) چاپ می‌شن تا بشه تنظیمشون کرد
DROP_NO_SPEECH_PROB = 0.6
LOW_LOGPROB = -1.0
POOR_MEAN_LOGPROB, FAIR_MEAN_LOGPROB = -0.95, -0.65
POOR_LOW_RATIO, FAIR_LOW_RATIO = 0.45, 0.20
POOR_DROPPED_RATIO, FAIR_DROPPED_RATIO = 0.50, 0.25
PARAGRAPH_GAP_SECONDS = 1.5
# بخش‌هایی که Whisper بهشون اطمینان کمی داشته با [؟ ... ] علامت می‌خورن (آستانه‌ی تجربی؛ قابل‌تنظیم)
UNCERTAIN_LOGPROB = -0.9
UNCERTAIN_OPEN, UNCERTAIN_CLOSE = "[؟ ", "]"

BASE_PROMPT = "این یک جلسه‌ی درس دانشگاهی به زبان فارسی است. متن را با املای درست فارسی و نقطه‌گذاری بنویس."


def is_audio(content_type: str, filename: str) -> bool:
    if content_type in AUDIO_CONTENT_TYPES:
        return True
    return Path(filename).suffix.lower() in AUDIO_EXTENSIONS


def can_preprocess() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


CLEANUP_SYSTEM_PROMPT = (
    "متنی که بهت می‌دم بخشی از خروجی خام یه سیستم تبدیل گفتار-به-متن فارسیه (یه جلسه‌ی درس) "
    "و ممکنه غلط املایی، فاصله‌گذاری نادرست و بی‌نقطه‌گذاری داشته باشه.\n\n"
    "وظیفه‌ت:\n"
    "۱. غلط‌های املایی و فاصله‌گذاری (مثل نیم‌فاصله) رو اصلاح کن و نقطه‌گذاری و پاراگراف‌بندی مناسب اضافه کن.\n"
    "۲. فقط وقتی یه کلمه رو عوض کن که از خودِ همون جمله و جمله‌های کناریش کاملاً معلوم باشه "
    "منظور گوینده چی بوده (مثلاً اشتباه شنیدنِ دو کلمه‌ی هم‌آوا). از دانش بیرونی برای «ساختن» "
    "اصطلاح یا جمله‌ی جدید استفاده نکن.\n"
    "۳. هیچ مطلبی رو حذف، خلاصه یا تفسیر نکن و چیزی هم اضافه نکن. معنی رو عوض نکن.\n"
    "۴. جاهایی که نامفهومه یا مطمئن نیستی رو دست‌نخورده بذار و بعدش [؟] بنویس. حدس الکی نزن.\n"
    "۴-۱. متنی که به شکل [؟ ... ] اومده، بخشیه که سیستم تبدیل صدا بهش اطمینان نداشته. همون علامت [؟ ... ] رو دورِ "
    "متنِ متناظر نگه دار؛ محتواش رو فقط برای غلط املایی و نیم‌فاصله اصلاح کن، کامل یا بازنویسی‌ش نکن و حذفش هم نکن.\n"
    "۵. فقط متن اصلاح‌شده رو برگردون، بدون توضیح یا مقدمه.\n"
    "۶. کلمه‌ی انگلیسی یا زبان دیگه وسط متن فارسی نذار؛ فقط اسامی خاص و اختصارات بدون معادل "
    "فارسی (مثل CBT، DSM) می‌تونن لاتین بمونن."
)


# ---------- ffmpeg ----------
async def _run(*args: str, timeout: int = FFMPEG_TIMEOUT) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


def denoise_mode() -> str:
    mode = os.environ.get("STT_DENOISE", "off").strip().lower()
    return mode if mode in DENOISE_FILTERS else "off"


def audio_filter(mode: str) -> str:
    """حذف رامبل → (کاهش نویز) → یکدست‌کردن بلندی؛ نرمال‌سازی بعد از حذف نویز میاد تا نویز تقویت نشه."""
    parts = ["highpass=f=70", DENOISE_FILTERS[mode], "dynaudnorm=f=250:g=15"]
    return ",".join(p for p in parts if p)


async def _convert(src: Path, workdir: Path) -> Path | None:
    """mono/16kHz + (کاهش نویز) + یکدست‌سازی بلندی + فشرده‌سازی (اول opus، اگه نشد mp3)."""
    for ext, codec in ((".ogg", ["-c:a", "libopus", "-b:a", "24k"]), (".mp3", ["-c:a", "libmp3lame", "-b:a", "40k"])):
        out = workdir / f"prepared{ext}"
        rc, _, err = await _run(
            "ffmpeg", "-y", "-nostdin", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
            "-af", audio_filter(denoise_mode()), *codec, str(out),
        )
        if rc == 0 and out.exists() and out.stat().st_size > 1000:
            return out
        logger.warning("ffmpeg convert (%s) failed: %s", ext, err[-300:])
    return None


async def _probe_duration(path: Path) -> float | None:
    rc, out, _ = await _run(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path), timeout=120
    )
    try:
        return float(out.strip()) if rc == 0 else None
    except ValueError:
        return None


async def _silence_ends(path: Path) -> list[float]:
    _, _, err = await _run(
        "ffmpeg", "-nostdin", "-i", str(path), "-af", "silencedetect=noise=-35dB:d=0.5", "-f", "null", "-"
    )
    return [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", err)]


def cut_points(duration: float, silence_ends: list[float]) -> list[float]:
    """نقطه‌های برش: نزدیک‌ترین سکوت به هر ۸ دقیقه (یا خودِ ۸ دقیقه اگه سکوتی نبود)."""
    points: list[float] = []
    last = 0.0
    while duration - last > SEGMENT_MAX_SECONDS:
        target = last + SEGMENT_TARGET_SECONDS
        window = [s for s in silence_ends if abs(s - target) <= SPLIT_SEARCH_WINDOW and s > last + 60]
        cut = min(window, key=lambda s: abs(s - target)) if window else target
        points.append(cut)
        last = cut
    return points


async def _split(path: Path, points: list[float], duration: float, workdir: Path) -> list[Path] | None:
    bounds = [0.0, *points, duration]
    parts: list[Path] = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        out = workdir / f"part{i:03d}{path.suffix}"
        rc, _, err = await _run(
            "ffmpeg", "-y", "-nostdin", "-ss", f"{a:.2f}", "-t", f"{b - a:.2f}", "-i", str(path), "-c", "copy", str(out)
        )
        if rc != 0 or not out.exists() or out.stat().st_size < 500:
            logger.warning("ffmpeg split failed at part %s: %s", i, err[-200:])
            return None
        parts.append(out)
    return parts


# ---------- Groq ----------
async def _groq_transcribe(path: Path, filename: str, prompt: str) -> dict:
    last_err: Exception | None = None
    for delay in RETRY_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                with open(path, "rb") as f:
                    resp = await client.post(
                        GROQ_TRANSCRIPTION_URL,
                        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                        data={
                            "model": WHISPER_MODEL,
                            "language": "fa",
                            "temperature": "0",
                            "response_format": "verbose_json",
                            "prompt": prompt,
                        },
                        files={"file": (filename, f)},
                    )
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = httpx.HTTPStatusError(f"Groq {resp.status_code}", request=resp.request, response=resp)
                logger.warning("Groq returned %s (will retry)", resp.status_code)
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.TransportError as e:
            last_err = e
            logger.warning("Groq transport error (will retry): %s", e)
    raise last_err or RuntimeError("Groq transcription failed")


_LOOP_RE = re.compile(r"((?:\S+\s+){1,12}?)\1{2,}")


def collapse_repeats(text: str) -> str:
    """حلقه‌ی تکرارِ Whisper («نباید ناراحت بشن» چند بار پشت هم) رو به یه بار تبدیل می‌کنه."""
    prev = None
    while prev != text:
        prev = text
        text = _LOOP_RE.sub(r"\1", text + " ").strip()
    return text


def new_stats() -> dict:
    return {"kept": 0.0, "dropped": 0.0, "low": 0.0, "lp_sum": 0.0, "lp_dur": 0.0, "segments": 0}


def consume_response(data: dict, stats: dict) -> str:
    """متنِ تمیزشده‌ی یه پاسخ verbose_json رو برمی‌گردونه و آمار کیفیت رو به‌روز می‌کنه."""
    segments = data.get("segments") or []
    if not segments:
        return collapse_repeats((data.get("text") or "").strip())

    pieces: list[str] = []
    prev_end = None
    for seg in segments:
        text = (seg.get("text") or "").strip()
        start, end = float(seg.get("start") or 0), float(seg.get("end") or 0)
        dur = max(end - start, 0.01)
        logprob = seg.get("avg_logprob")
        no_speech = seg.get("no_speech_prob") or 0.0
        if not text or (no_speech > DROP_NO_SPEECH_PROB and logprob is not None and logprob < LOW_LOGPROB):
            stats["dropped"] += dur  # توهمِ سکوت یا بخش خالی
            continue
        stats["kept"] += dur
        stats["segments"] += 1
        if logprob is not None:
            stats["lp_sum"] += logprob * dur
            stats["lp_dur"] += dur
            if logprob < LOW_LOGPROB:
                stats["low"] += dur
        text = collapse_repeats(text)
        if logprob is not None and logprob < UNCERTAIN_LOGPROB:
            text = f"{UNCERTAIN_OPEN}{text}{UNCERTAIN_CLOSE}"
            stats["flagged"] = stats.get("flagged", 0) + 1
        sep = "\n\n" if prev_end is not None and start - prev_end > PARAGRAPH_GAP_SECONDS else " "
        pieces.append((sep if pieces else "") + text)
        prev_end = end
    return "".join(pieces).strip()


def quality_from_stats(stats: dict) -> dict:
    total = stats["kept"] + stats["dropped"]
    info = {
        "level": None,
        "mean_logprob": None,
        "low_ratio": None,
        "dropped_ratio": round(stats["dropped"] / total, 3) if total else None,
        "segments": stats["segments"],
        "flagged_segments": stats.get("flagged", 0),
        "seconds": round(total),
    }
    if stats["lp_dur"] <= 0:
        return info
    mean = stats["lp_sum"] / stats["lp_dur"]
    low = stats["low"] / stats["lp_dur"]
    dropped = info["dropped_ratio"] or 0.0
    info["mean_logprob"], info["low_ratio"] = round(mean, 3), round(low, 3)
    if mean < POOR_MEAN_LOGPROB or low > POOR_LOW_RATIO or dropped > POOR_DROPPED_RATIO:
        info["level"] = "poor"
    elif mean < FAIR_MEAN_LOGPROB or low > FAIR_LOW_RATIO or dropped > FAIR_DROPPED_RATIO:
        info["level"] = "fair"
    else:
        info["level"] = "good"
    return info


# ---------- پاک‌سازی ----------
async def cleanup_text(raw_text: str) -> str:
    """تکه‌تکه (سر جمله‌ها) پاک‌سازی می‌کنه. اگه خروجی یه تکه خالی یا بی‌ربط به طول ورودی باشه
    (مثلاً بریده‌شده)، همون تکه‌ی خام نگه داشته می‌شه تا مطلبی گم نشه."""
    try:
        provider = get_ai_provider()
    except HTTPException:
        return raw_text
    out: list[str] = []
    changed = False
    for chunk in fullnotes.split_chunks(raw_text):
        try:
            cleaned = ((await call_ai_safely(provider, chunk, system=CLEANUP_SYSTEM_PROMPT)) or "").strip()
        except Exception:
            cleaned = ""
        ok = bool(cleaned) and 0.7 * len(chunk) <= len(cleaned) <= 1.6 * len(chunk)
        if ok and chunk.count(UNCERTAIN_OPEN.strip()) > cleaned.count(UNCERTAIN_OPEN.strip()) * 1.5 + 1:
            ok = False  # علامت‌های «کم‌اطمینان» از بین رفتن؛ خروجی قابل‌اعتماد نیست
        changed = changed or ok
        out.append(cleaned if ok else chunk)
    return "\n\n".join(out) if changed else raw_text  # اگه هیچ تکه‌ای قابل‌اعتماد نبود، متن اصلی با پاراگراف‌هاش دست‌نخورده برمی‌گرده


# ---------- ورودی اصلی ----------
async def transcribe_audio_detailed(file_path: Path, filename: str, hint: str | None = None) -> tuple[str, dict]:
    """برمی‌گردونه: (متن، اطلاعات کیفیت). hint (مثلاً اسم درس) به Whisper کمک می‌کنه اصطلاح‌ها رو درست بشنوه."""
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")

    prompt = BASE_PROMPT + (f" موضوع درس: {hint.strip()[:80]}." if hint and hint.strip() else "")
    preprocessed = False
    stats = new_stats()
    pieces: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        parts, names = [file_path], [filename]
        if can_preprocess():
            try:
                prepared = await _convert(file_path, workdir)
                if prepared:
                    preprocessed = True
                    parts, names = [prepared], [prepared.name]
                    duration = await _probe_duration(prepared)
                    if duration and duration > SEGMENT_MAX_SECONDS:
                        points = cut_points(duration, await _silence_ends(prepared))
                        split = await _split(prepared, points, duration, workdir)
                        if split:
                            parts, names = split, [p.name for p in split]
            except Exception:
                logger.exception("Audio preprocessing failed; using the original file")
                parts, names, preprocessed = [file_path], [filename], False

        for part, name in zip(parts, names):
            data = await _groq_transcribe(part, name, prompt)
            text = consume_response(data, stats)
            if text:
                pieces.append(text)

    raw_text = "\n\n".join(pieces).strip()
    info = quality_from_stats(stats)
    info.update(preprocessed=preprocessed, parts=len(parts), cleaned=False, denoise=denoise_mode() if preprocessed else None)
    logger.info("Transcription quality for %s: %s", filename, info)

    if not raw_text:
        return raw_text, info
    # کیفیت خوب نیازی به پاک‌سازی نداره؛ کیفیت بد هم دست‌نخورده می‌مونه تا مدل متنِ ساختگی نسازه
    if info["level"] in ("fair", None):
        cleaned = await cleanup_text(raw_text)
        info["cleaned"] = cleaned != raw_text
        raw_text = cleaned
    return raw_text.strip(), info


async def transcribe_audio(file_path: Path, filename: str) -> str:
    """سازگاری با کدِ قبلی: فقط متن."""
    text, _ = await transcribe_audio_detailed(file_path, filename)
    return text
