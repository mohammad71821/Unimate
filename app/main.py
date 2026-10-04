import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.routers import admin, ai, auth, chat, flashcards, jozve, notes, redeem, reminders, webapp

_background_tasks: list = []  # نگه‌داشتن رفرنس تسک‌ها تا garbage collect نشن


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from bot import run_bot_async
        asyncio.create_task(run_bot_async())
    except Exception as e:
        print(f"Failed to start bot background task: {e}")
    # ربات بله فقط وقتی روشن می‌شه که BALE_BOT_TOKEN تو Environment ست شده باشه؛ خطاش ربات تلگرام رو خراب نمی‌کنه
    if os.environ.get("BALE_BOT_TOKEN"):
        try:
            from bale_bot import run_bot_async as run_bale_bot_async

            _background_tasks.append(asyncio.create_task(run_bale_bot_async()))
        except Exception as e:
            print(f"Failed to start Bale bot background task: {e}")
    yield

app = FastAPI(title="UniMate AI", version="0.1.0", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(notes.router)
app.include_router(ai.router)
app.include_router(chat.router)
app.include_router(reminders.router)
app.include_router(admin.router)
app.include_router(redeem.router)
app.include_router(flashcards.router)
app.include_router(jozve.router)
app.include_router(webapp.router)

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "unimate-ai"}
