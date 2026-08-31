from fastapi import FastAPI

from app.routers import admin, ai, auth, chat, flashcards, notes, redeem, reminders, webapp

app = FastAPI(title="UniMate AI", version="0.1.0")

app.include_router(auth.router)
app.include_router(notes.router)
app.include_router(ai.router)
app.include_router(chat.router)
app.include_router(reminders.router)
app.include_router(admin.router)
app.include_router(redeem.router)
app.include_router(flashcards.router)
app.include_router(webapp.router)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "unimate-ai"}
