#!/data/data/com.termux/files/usr/bin/bash
# نگهبان سلامت: هر ۶۰ ثانیه چک می‌کنه API، بات، و تونل ngrok واقعاً
# پاسخ‌گو باشن. برخلاف حلقه‌ی خودترمیمِ داخل start.sh (که فقط کرشِ کامل رو
# می‌گیره)، این اسکریپت حالت «گیرکردن/هنگ» (پردازش زنده‌ست ولی جواب نمی‌ده)
# رو هم تشخیص می‌ده و با کشتنِ همون پردازشِ خاص، باعث می‌شه حلقه‌ی موجود
# خودش دوباره بالاش بیاره.

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEARTBEAT_FILE="$PROJECT_DIR/bot_heartbeat.txt"
CHECK_INTERVAL=60
BOT_HEARTBEAT_MAX_AGE=180   # اگه بات این‌قدر ثانیه ضربان نداشت، یعنی گیر کرده

log() { echo "[watchdog] $(date '+%F %T') $1"; }

log "started — checking every ${CHECK_INTERVAL}s"

while true; do
    sleep "$CHECK_INTERVAL"

    # --- 1) سلامت API ---
    if ! curl -sf --max-time 10 http://127.0.0.1:8000/health > /dev/null 2>&1; then
        log "API not responding — killing it so it can restart"
        pkill -f "uvicorn app.main:app"
    fi

    # --- 2) ضربان حیات بات ---
    if [ -f "$HEARTBEAT_FILE" ]; then
        last=$(cut -d. -f1 < "$HEARTBEAT_FILE" 2>/dev/null)
        now=$(date +%s)
        if [ -n "$last" ] && [ $((now - last)) -gt "$BOT_HEARTBEAT_MAX_AGE" ]; then
            log "bot heartbeat stale ($((now - last))s old) — killing it so it can restart"
            pkill -f "python bot.py"
        fi
    fi

    # --- 3) تونل ngrok (فقط اگه توی .env تنظیم شده باشه) ---
    WEBAPP_URL=$(grep '^WEBAPP_URL=' "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-)
    if [ -n "$WEBAPP_URL" ]; then
        if ! curl -sf --max-time 10 "$WEBAPP_URL" > /dev/null 2>&1; then
            log "ngrok tunnel not responding — killing it so it can restart"
            pkill -f "ngrok http"
        fi
    fi
done
