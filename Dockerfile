FROM python:3.12-slim

# ابزارهای سیستمی لازم:
# - tesseract-ocr + بسته‌ی زبان فارسی: برای OCR روی عکس جزوه
# - ffmpeg: برای بریدن فایل‌های صوتی بزرگ قبل از ارسال به Groq
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-fas \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# این دستور پیش‌فرضِ سرویس API ـه (migration + اجرای سرور).
# برای سرویسِ بات، توی تنظیمات Railway این‌و override می‌کنیم به: python bot.py
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
