#!/data/data/com.termux/files/usr/bin/bash
set -uo pipefail

SESSION="unimate"
PG_DATA="$PREFIX/var/lib/postgresql"

echo "== UniMate AI: stopping =="

if tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux kill-session -t "$SESSION"
    echo "[ok] tmux session '$SESSION' stopped (API + bot)"
else
    echo "[ok] no tmux session '$SESSION' running"
fi

read -p "Postgres رو هم متوقف کنم؟ (y/n) " ans
if [ "$ans" = "y" ]; then
    pg_ctl -D "$PG_DATA" stop
    echo "[ok] Postgres stopped"
fi
