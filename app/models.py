import uuid
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, func, BigInteger, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    plan: Mapped[str] = mapped_column(String(20), default="free")
    credits: Mapped[int] = mapped_column(default=0)  # اعتبار انباشتیِ همیشگی — فقط با پاداش دعوت یا شارژ دستی زیاد می‌شه
    premium_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_flashcard_nudge_date: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # سهمیه‌ی روزانه‌ی رایگان: هر روز تا daily_credits_date عوض نشه (یعنی روز عوض بشه)، از نو ۱۰ تا می‌شه
    daily_credits_used: Mapped[int] = mapped_column(default=0)
    daily_credits_date: Mapped[str | None] = mapped_column(String(10), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    original_filename: Mapped[str] = mapped_column(String(500))
    storage_key: Mapped[str] = mapped_column(String(500))
    file_size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(150))
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_status: Mapped[str] = mapped_column(String(50), default="pending")
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notes.id"), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" or "assistant"
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    chat_id: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), index=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Flashcard(Base):
    """
    فلش‌کارتی که با الگوریتم SM-2 (همون الگوریتم پایه‌ی Anki) زمان‌بندی می‌شه.
    هر بار که کاربر مرورش می‌کنه، interval_days و ease_factor آپدیت می‌شن تا
    فاصله‌ی مرور بعدی بر اساس عملکرد واقعی کاربر تنظیم بشه.
    """

    __tablename__ = "flashcards"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notes.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)

    interval_days: Mapped[float] = mapped_column(default=1.0)
    ease_factor: Mapped[float] = mapped_column(default=2.5)
    repetitions: Mapped[int] = mapped_column(default=0)

    next_review_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RedeemCode(Base):
    __tablename__ = "redeem_codes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    credits: Mapped[int] = mapped_column(default=0)
    grants_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    premium_days: Mapped[int | None] = mapped_column(nullable=True)  # None یعنی دائمی
    max_uses: Mapped[int] = mapped_column(default=1)
    times_used: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RedeemCodeUse(Base):
    """کدام کاربر کدام کد رو کِی استفاده کرده — برای جلوگیری از استفاده‌ی دوباره‌ی همون کاربر از یه کد."""

    __tablename__ = "redeem_code_uses"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("redeem_codes.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Referral(Base):
    """
    ثبت این‌که کدوم کاربر (referrer) کدوم کاربر جدید (referred) رو دعوت کرده.
    هر کاربر فقط یه‌بار می‌تونه به‌عنوان «دعوت‌شده» ثبت بشه (برای جلوگیری از
    ثبت تکراری/سوءاستفاده)، دقیقاً همون لحظه‌ای که برای اولین‌بار عضو بات می‌شه.
    """

    __tablename__ = "referrals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    referrer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    referred_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
