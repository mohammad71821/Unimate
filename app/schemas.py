import uuid

from pydantic import BaseModel, EmailStr


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str
    password: str


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class NoteFromText(BaseModel):
    text: str


class SearchQuery(BaseModel):
    query: str


class TelegramAuthRequest(BaseModel):
    telegram_user_id: str
    bot_secret: str
    referred_by: str | None = None  # telegram_user_id کاربری که این کاربر رو دعوت کرده (اگه از لینک دعوت اومده باشه)


class ReminderCreate(BaseModel):
    chat_id: str
    message: str
    remind_at: str  # ISO 8601, e.g. "2026-07-25T18:00:00"


class DueReminderCheck(BaseModel):
    bot_secret: str


class StudyPlanRequest(BaseModel):
    days: int


class UserOut(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    is_active: bool

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ChatMessageIn(BaseModel):
    message: str


class ChatMessageOut(BaseModel):
    role: str
    content: str


class GrantCreditsRequest(BaseModel):
    admin_secret: str
    email: EmailStr
    credits_to_add: int = 0
    set_plan: str | None = None  # "free" or "premium"


class AdminUserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    plan: str
    credits: int
    is_active: bool
    notes_count: int
    telegram_id: str | None = None
    created_at: str
    premium_until: str | None = None  # None + plan=premium یعنی دائمی


class AdminCreditsUpdate(BaseModel):
    delta: int  # می‌تونه منفی هم باشه (کسر اعتبار)


class AdminPlanUpdate(BaseModel):
    plan: str  # "free" یا "premium"
    days: int | None = None  # فقط وقتی plan=="premium" معنی داره؛ None یعنی دائمی


class AdminActiveUpdate(BaseModel):
    is_active: bool


class AdminStatsOut(BaseModel):
    total_users: int
    premium_users: int
    free_users: int
    inactive_users: int
    total_notes: int
    total_credits_outstanding: int


class RedeemCodeCreate(BaseModel):
    credits: int = 0
    grants_premium: bool = False
    premium_days: int | None = None  # None یعنی دائمی (فقط وقتی grants_premium=True معنی داره)
    max_uses: int = 1
    quantity: int = 1  # چند تا کد جدا با همین تنظیمات ساخته بشه


class RedeemCodeOut(BaseModel):
    code: str
    credits: int
    grants_premium: bool
    premium_days: int | None = None
    max_uses: int
    times_used: int
    is_active: bool
    created_at: str


class RedeemRequest(BaseModel):
    code: str


class SlidesRequest(BaseModel):
    slide_count: int = 8  # شامل اسلاید عنوانه؛ محدوده‌ی مجاز در endpoint چک می‌شه


class FlashcardOut(BaseModel):
    id: uuid.UUID
    note_id: uuid.UUID
    question: str
    answer: str
    repetitions: int
    next_review_at: str


class FlashcardReviewRequest(BaseModel):
    rating: str  # "again" | "hard" | "good"


class FlashcardReviewResult(BaseModel):
    id: uuid.UUID
    interval_days: float
    next_review_at: str
    remaining_due: int


class RedeemResult(BaseModel):
    plan: str
    credits: int
    premium_until: str | None = None
    message: str


class FlashcardItem(BaseModel):
    question: str
    answer: str


class FlashcardsSaveRequest(BaseModel):
    note_id: uuid.UUID
    cards: list[FlashcardItem]


class FlashcardsPdfRequest(BaseModel):
    cards: list[FlashcardItem]


class SendTextToChatRequest(BaseModel):
    title: str | None = None
    text: str
