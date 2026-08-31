import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.gateway import call_ai_safely, get_ai_provider
from app.database import get_db
from app.deps import consume_credit, get_current_user
from app.models import ChatMessage, ChatSession, Note, User
from app.schemas import ChatMessageIn, ChatMessageOut

router = APIRouter(prefix="/notes", tags=["chat"])

CHAT_SYSTEM_PROMPT = (
    "You are a helpful academic assistant. Answer the user's questions based ONLY on "
    "the study material provided below. If the answer is not in the material, say so clearly. "
    "Answer in the same language the user asks in.\n\n"
    "--- STUDY MATERIAL ---\n{note_text}\n--- END OF STUDY MATERIAL ---"
)


async def _get_owned_note(note_id: uuid.UUID, current_user: User, db: AsyncSession) -> Note:
    note = await db.scalar(select(Note).where(Note.id == note_id))
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your note")
    if not note.extracted_text:
        raise HTTPException(status_code=400, detail="No extracted text available for this note")
    return note


async def _get_or_create_session(note_id: uuid.UUID, current_user: User, db: AsyncSession) -> ChatSession:
    session = await db.scalar(
        select(ChatSession).where(
            ChatSession.note_id == note_id, ChatSession.owner_id == current_user.id
        )
    )
    if session:
        return session
    session = ChatSession(note_id=note_id, owner_id=current_user.id)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


@router.post("/{note_id}/chat", response_model=ChatMessageOut)
async def chat_with_note(
    note_id: uuid.UUID,
    payload: ChatMessageIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = await _get_owned_note(note_id, current_user, db)
    session = await _get_or_create_session(note_id, current_user, db)

    history_result = await db.scalars(
        select(ChatMessage).where(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at)
    )
    history = history_result.all()

    conversation_lines = [f"{m.role}: {m.content}" for m in history]
    conversation_lines.append(f"user: {payload.message}")
    full_prompt = "\n".join(conversation_lines)

    system_prompt = CHAT_SYSTEM_PROMPT.format(note_text=note.extracted_text[:6000])

    provider = get_ai_provider()
    answer = await call_ai_safely(provider, prompt=full_prompt, system=system_prompt)

    # فقط بعد از گرفتن جواب موفق، هم پیام‌ها رو ذخیره می‌کنیم هم اعتبار کسر می‌کنیم —
    # اگه AI خطا بده، نه پیامی توی تاریخچه می‌مونه نه اعتباری از دست می‌ره
    user_msg = ChatMessage(session_id=session.id, role="user", content=payload.message)
    assistant_msg = ChatMessage(session_id=session.id, role="assistant", content=answer)
    db.add(user_msg)
    db.add(assistant_msg)
    await consume_credit(current_user, db)
    await db.commit()

    return ChatMessageOut(role="assistant", content=answer)


@router.get("/{note_id}/chat/history")
async def get_chat_history(
    note_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_owned_note(note_id, current_user, db)
    session = await db.scalar(
        select(ChatSession).where(
            ChatSession.note_id == note_id, ChatSession.owner_id == current_user.id
        )
    )
    if not session:
        return {"messages": []}

    result = await db.scalars(
        select(ChatMessage).where(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at)
    )
    return {"messages": [{"role": m.role, "content": m.content} for m in result.all()]}
