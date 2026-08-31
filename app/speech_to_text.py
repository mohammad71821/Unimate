from pathlib import Path

import httpx

from app.ai.gateway import call_ai_safely, get_ai_provider
from app.config import settings
from fastapi import HTTPException

GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

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


def is_audio(content_type: str, filename: str) -> bool:
    if content_type in AUDIO_CONTENT_TYPES:
        return True
    return Path(filename).suffix.lower() in AUDIO_EXTENSIONS


CLEANUP_SYSTEM_PROMPT = (
    "متنی که بهت می‌دم خروجی خام یه سیستم تبدیل گفتار-به-متن (Speech-to-Text) فارسیه، "
    "احتمالاً یه یادداشت آموزشی/بالینی (مثل روان‌پزشکی، پزشکی، یا سایر رشته‌های علمی) "
    "و پر از غلط‌های تایپی، اصطلاحات تخصصی اشتباه تشخیص داده‌شده، و بی‌نقطه‌گذاریه.\n\n"
    "وظیفه‌ت اینه که:\n"
    "۱. غلط‌های تایپی و اصطلاحات نادرست رو اصلاح کنی — نه فقط غلط‌های واضح، بلکه "
    "اصطلاحات تخصصی‌ای که STT به‌خاطر شباهت آوایی اشتباه نوشته (مثلاً یه کلمه‌ی بی‌معنی یا "
    "غیرمرتبط که با دانش تخصصی حوزه‌ی متن، معلومه باید یه اصطلاح رایج و شناخته‌شده باشه). "
    "از دانش تخصصی خودت (مثلاً معیارهای تشخیصی DSM، اصطلاحات پزشکی رایج) برای تشخیص این "
    "موارد استفاده کن، حتی اگه کلمه‌ی نوشته‌شده هیچ شباهت نوشتاری به کلمه‌ی درست نداشته باشه.\n"
    "۲. نقطه‌گذاری و پاراگراف‌بندی مناسب اضافه کنی.\n"
    "۳. هیچ محتوا، معنی، یا اطلاعاتی که واقعاً گفته شده رو حذف یا تفسیر نکن و چیزی به محتوا "
    "اضافه نکن — فقط کلمات نادرست رو به معادل درستشون تبدیل کن، مفاهیم رو تغییر نده.\n"
    "۴. اگه بخشی از جمله بعد از تلاش برای اصلاح هم نامفهوم موند و مطمئن نبودی، همون رو "
    "دست‌نخورده نگه دار (حدس الکی نزن).\n"
    "۵. فقط و فقط متن اصلاح‌شده رو برگردون، بدون هیچ توضیح یا مقدمه‌ی اضافه.\n"
    "۶. هیچ کلمه یا عبارتی از زبون‌های دیگه (انگلیسی، چینی، ترکی، فرانسوی، و غیره) رو "
    "وسط متن فارسی جا نذار — همیشه معادل فارسیِ رایج کلمه رو بنویس، حتی اگه Whisper اون "
    "کلمه رو به یه زبون دیگه تشخیص داده باشه. فقط اسامی خاص یا اختصارات بدون معادل فارسی "
    "(مثل CBT، DSM) می‌تونن لاتین بمونن."
)


async def transcribe_audio(file_path: Path, filename: str) -> str:
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")

    async with httpx.AsyncClient(timeout=600) as client:
        with open(file_path, "rb") as f:
            resp = await client.post(
                GROQ_TRANSCRIPTION_URL,
                headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                data={"model": "whisper-large-v3", "language": "fa"},
                files={"file": (filename, f)},
            )
        resp.raise_for_status()
        raw_text = resp.json()["text"].strip()

    if not raw_text:
        return raw_text

    try:
        provider = get_ai_provider()
        cleaned = await call_ai_safely(provider, raw_text, system=CLEANUP_SYSTEM_PROMPT)
        return cleaned.strip()
    except HTTPException:
        return raw_text
