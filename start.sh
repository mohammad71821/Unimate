#!/data/data/com.termux/files/usr/bin/bash
set -uo pipefail

PROJECT_DIR="$HOME/unimate-ai"
SESSION="unimate"
PG_DATA="$PREFIX/var/lib/postgresql"
PG_LOG="$PG_DATA/log"

echo "== UniMate AI: starting everything =="

# --- 0) Wake lock: جلوگیری از کشتن Termux توسط اندروید در پس‌زمینه ---
termux-wake-lock 2>/dev/null || true

# --- 1) Postgres ---
if pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
    echo "[ok] Postgres already running (accepting connections)"
else
    echo "[..] Starting Postgres"
    if [ -f "$PG_DATA/postmaster.pid" ]; then
        rm -f "$PG_DATA/postmaster.pid"
    fi
    pg_ctl -D "$PG_DATA" -l "$PG_LOG" start
    sleep 2
    if pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
        echo "[ok] Postgres started"
    else
        echo "[FAIL] Postgres did not start — check $PG_LOG"
        tail -n 20 "$PG_LOG"
        exit 1
    fi
fi

# --- 2) tmux session with API + bot ---
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "[ok] tmux session '$SESSION' already exists — not restarting API/bot"
    echo "     (use ./stop.sh first if you want a clean restart)"
else
    echo "[..] Creating tmux session '$SESSION'"
    tmux new-session -d -s "$SESSION" -n api \
        "cd '$PROJECT_DIR' && source venv/bin/activate && while true; do uvicorn app.main:app --host 127.0.0.1 --port 8000; echo '[api] crashed — restarting in 5s...'; sleep 5; done"

    tmux new-window -t "$SESSION" -n bot \
        "cd '$PROJECT_DIR' && source venv/bin/activate && while true; do python bot.py; echo '[bot] crashed — restarting in 5s...'; sleep 5; done"

    echo "[ok] Started 'api' and 'bot' windows inside tmux session '$SESSION' (self-healing on crash)"
fi

# --- 3) ngrok tunnel (برای مینی‌اپ) ---
# جدا از این‌که session جدید ساخته شده باشه یا از قبل بوده، مطمئن می‌شیم
# پنجره‌ی ngrok هم زندست — چون این دقیقاً همون چیزی بود که هر بار از قلم می‌افتاد
NGROK_DOMAIN="query-shimmy-relish.ngrok-free.dev"
if tmux list-windows -t "$SESSION" 2>/dev/null | grep -q "ngrok"; then
    echo "[ok] tmux window 'ngrok' already running"
else
    echo "[..] Starting ngrok tunnel window"
    tmux new-window -t "$SESSION" -n ngrok \
        "while true; do ngrok http --url=https://$NGROK_DOMAIN 8000; echo '[ngrok] crashed — restarting in 5s...'; sleep 5; done"
    echo "[ok] Started 'ngrok' window (self-healing on crash)"
fi

# --- 4) Watchdog (تشخیص هنگ‌کردن، نه فقط کرش کامل) ---
if tmux list-windows -t "$SESSION" 2>/dev/null | grep -q "watchdog"; then
    echo "[ok] tmux window 'watchdog' already running"
else
    echo "[..] Starting watchdog window"
    tmux new-window -t "$SESSION" -n watchdog \
        "while true; do bash '$PROJECT_DIR/watchdog.sh'; echo '[watchdog] crashed — restarting in 5s...'; sleep 5; done"
    echo "[ok] Started 'watchdog' window (self-healing on crash)"
fi

echo ""
echo "همه‌چیز روشنه. برای دیدن لاگ‌ها:"
echo "  tmux attach -t $SESSION"
echo "برای رفتن بین پنجره‌ها: Ctrl+b بعد عدد پنجره (0=api, 1=bot, 2=ngrok, 3=watchdog)"
echo "برای خروج بدون بستن: Ctrl+b بعد d"
