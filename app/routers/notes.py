import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.embeddings import cosine_similarity, get_embedding, get_query_embedding
from app.models import Note, User
from app.ocr import extract_text_from_image, is_image
from app.pptx_extraction import extract_text_from_pptx, is_pptx
from app.schemas import NoteFromText, SearchQuery, SendTextToChatRequest
from app.config import settings
import httpx
from app.routers.ai import _telegram_chat_id_from_user
from app.speech_to_text import is_audio, transcribe_audio
from app.storage import storage_backend
from app.text_extraction import extract_text_from_pdf

logger = logging.getLogger("unimate.notes")
router = APIRouter(prefix="/notes", tags=["notes"])

MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 30MB


async def _try_store_embedding(note: Note, db: AsyncSession) -> None:
    if note.processing_status != "done" or not note.extracted_text:
        return
    try:
        vector = await get_embedding(note.extracted_text)
        note.embedding = json.dumps(vector)
        await db.commit()
    except Exception:
        logger.exception("Embedding failed for note %s", note.id)


@router.post("/upload")
async def upload_note(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    key = storage_backend.save_file(file.file, file.filename)
    file_path = storage_backend.get_file_path(key)
    size = file_path.stat().st_size

    if size > MAX_UPLOAD_BYTES:
        storage_backend.delete_file(key)
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max allowed size is {MAX_UPLOAD_BYTES // (1024 * 1024)}MB.",
        )

    content_type = file.content_type or "application/octet-stream"

    extracted_text = None
    processing_status = "not_supported"

    if content_type == "application/pdf" or file.filename.lower().endswith(".pdf"):
        try:
            extracted_text = extract_text_from_pdf(file_path)
            processing_status = "done"
        except Exception:
            processing_status = "failed"
    elif is_pptx(content_type, file.filename):
        try:
            extracted_text = extract_text_from_pptx(file_path)
            processing_status = "done"
        except Exception:
            logger.exception("PPTX extraction failed for %s", file.filename)
            processing_status = "failed"
    elif is_audio(content_type, file.filename):
        try:
            if size > 25 * 1024 * 1024:
                raise ValueError("Audio file exceeds Groq's 25MB limit")
            extracted_text = await transcribe_audio(file_path, file.filename)
            processing_status = "done"
        except Exception:
            logger.exception("Audio transcription failed for %s", file.filename)
            processing_status = "failed"
    elif is_image(content_type, file.filename):
        try:
            extracted_text = extract_text_from_image(file_path)
            processing_status = "done"
        except Exception:
            logger.exception("OCR failed for %s", file.filename)
            processing_status = "failed"

    note = Note(
        owner_id=current_user.id,
        original_filename=file.filename,
        storage_key=key,
        file_size_bytes=size,
        content_type=content_type,
        extracted_text=extracted_text,
        processing_status=processing_status,
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)

    await _try_store_embedding(note, db)

    return {
        "id": str(note.id),
        "original_filename": note.original_filename,
        "size_bytes": note.file_size_bytes,
        "content_type": note.content_type,
        "processing_status": note.processing_status,
    }


@router.post("/from-text")
async def create_note_from_text(
    payload: NoteFromText,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is empty")

    note = Note(
        owner_id=current_user.id,
        original_filename="متن ارسالی",
        storage_key="",
        file_size_bytes=len(text.encode("utf-8")),
        content_type="text/plain",
        extracted_text=text,
        processing_status="done",
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)

    await _try_store_embedding(note, db)

    return {
        "id": str(note.id),
        "original_filename": note.original_filename,
        "size_bytes": note.file_size_bytes,
        "content_type": note.content_type,
        "processing_status": note.processing_status,
    }


@router.post("/search")
async def search_notes(
    payload: SearchQuery,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query is empty")

    result = await db.scalars(
        select(Note).where(
            Note.owner_id == current_user.id,
            Note.embedding.is_not(None),
        )
    )
    notes = result.all()
    if not notes:
        return {"results": []}

    query_vector = await get_query_embedding(query)

    scored = []
    for note in notes:
        try:
            note_vector = json.loads(note.embedding)
        except (TypeError, ValueError):
            continue
        score = cosine_similarity(query_vector, note_vector)
        scored.append((score, note))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:5]

    return {
        "results": [
            {
                "note_id": str(note.id),
                "filename": note.original_filename,
                "score": round(score, 4),
                "snippet": (note.extracted_text or "")[:300],
            }
            for score, note in top
        ]
    }


@router.get("/{note_id}")
async def get_note(
    note_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = await db.scalar(select(Note).where(Note.id == note_id))
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your note")
    return {
        "id": str(note.id),
        "original_filename": note.original_filename,
        "size_bytes": note.file_size_bytes,
        "content_type": note.content_type,
        "processing_status": note.processing_status,
        "extracted_text": note.extracted_text,
        "created_at": note.created_at.isoformat() if note.created_at else None,
    }


@router.get("/")
async def list_my_notes(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.scalars(
        select(Note).where(Note.owner_id == current_user.id).order_by(Note.created_at.desc())
    )
    notes = result.all()
    return [
        {
            "id": str(n.id),
            "original_filename": n.original_filename,
            "size_bytes": n.file_size_bytes,
            "content_type": n.content_type,
            "processing_status": n.processing_status,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in notes
    ]


@router.post("/send-text-to-chat")
async def send_text_to_chat(
    payload: SendTextToChatRequest,
    current_user: User = Depends(get_current_user),
):
    """
    متنی که کاربر توی مینی‌اپ گرفته (خلاصه/ترجمه/سؤالات) رو مستقیم به چت
    تلگرامش می‌فرسته — چون داخل WebView تلگرام نمی‌شه فایل/متن رو قابل‌اعتماد
    ذخیره یا دانلود کرد، ولی توی خودِ چت همیشه باقی می‌مونه.
    """
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=500, detail="Bot token روی سرور تنظیم نشده")
    chat_id = _telegram_chat_id_from_user(current_user)
    if not chat_id:
        raise HTTPException(status_code=400, detail="این حساب به تلگرام وصل نیست")

    full_text = f"📌 {payload.title}\n\n{payload.text}" if payload.title else payload.text
    chunks = [full_text[i:i + 3800] for i in range(0, len(full_text), 3800)] or [full_text]

    async with httpx.AsyncClient(timeout=30) as client:
        for chunk in chunks:
            resp = await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail="ارسال به چت با خطا مواجه شد.")

    return {"sent": True}
