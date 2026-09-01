import hmac
import secrets
import string
import uuid as uuid_module
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.deps import verify_admin_secret
from app.models import Note, RedeemCode, User
from app.schemas import (
    AdminActiveUpdate,
    AdminCreditsUpdate,
    AdminPlanUpdate,
    AdminStatsOut,
    AdminUserOut,
    GrantCreditsRequest,
    RedeemCodeCreate,
    RedeemCodeOut,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _telegram_id_from_email(email: str) -> str | None:
    if email.startswith("tg-") and email.endswith("@telegram.local"):
        return email[len("tg-") : -len("@telegram.local")]
    return None


async def _user_to_out(user: User, db: AsyncSession) -> AdminUserOut:
    notes_count = await db.scalar(select(func.count(Note.id)).where(Note.owner_id == user.id)) or 0
    return AdminUserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        plan=user.plan,
        credits=user.credits,
        is_active=user.is_active,
        notes_count=notes_count,
        telegram_id=_telegram_id_from_email(user.email),
        created_at=user.created_at.isoformat() if user.created_at else "",
        premium_until=user.premium_until.isoformat() if user.premium_until else None,
    )


@router.post("/grant-credits")
async def grant_credits(payload: GrantCreditsRequest, db: AsyncSession = Depends(get_db)):
    """
    نگه‌داشته‌شده برای سازگاری با نسخه‌ی قبلی. پنل جدید از endpointهای
    زیر (با هدر X-Admin-Secret) استفاده می‌کنه.
    """
    if not settings.admin_secret or not hmac.compare_digest(
        payload.admin_secret, settings.admin_secret
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin secret")

    user = await db.scalar(select(User).where(User.email == payload.email))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.credits_to_add:
        user.credits += payload.credits_to_add
    if payload.set_plan in ("free", "premium"):
        user.plan = payload.set_plan

    await db.commit()
    await db.refresh(user)

    return {"email": user.email, "plan": user.plan, "credits": user.credits}


@router.get("/users", response_model=list[AdminUserOut], dependencies=[Depends(verify_admin_secret)])
async def list_users(db: AsyncSession = Depends(get_db)):
    notes_count_subq = (
        select(Note.owner_id, func.count(Note.id).label("notes_count"))
        .group_by(Note.owner_id)
        .subquery()
    )
    rows = await db.execute(
        select(User, func.coalesce(notes_count_subq.c.notes_count, 0))
        .outerjoin(notes_count_subq, User.id == notes_count_subq.c.owner_id)
        .order_by(User.created_at.desc())
    )
    result = []
    for user, notes_count in rows.all():
        result.append(
            AdminUserOut(
                id=user.id,
                email=user.email,
                full_name=user.full_name,
                plan=user.plan,
                credits=user.credits,
                is_active=user.is_active,
                notes_count=notes_count,
                telegram_id=_telegram_id_from_email(user.email),
                created_at=user.created_at.isoformat() if user.created_at else "",
                premium_until=user.premium_until.isoformat() if user.premium_until else None,
            )
        )
    return result


@router.get("/stats", response_model=AdminStatsOut, dependencies=[Depends(verify_admin_secret)])
async def get_stats(db: AsyncSession = Depends(get_db)):
    total_users = await db.scalar(select(func.count(User.id))) or 0
    premium_users = await db.scalar(select(func.count(User.id)).where(User.plan == "premium")) or 0
    inactive_users = await db.scalar(select(func.count(User.id)).where(User.is_active.is_(False))) or 0
    total_notes = await db.scalar(select(func.count(Note.id))) or 0
    total_credits = await db.scalar(select(func.coalesce(func.sum(User.credits), 0))) or 0

    return AdminStatsOut(
        total_users=total_users,
        premium_users=premium_users,
        free_users=total_users - premium_users,
        inactive_users=inactive_users,
        total_notes=total_notes,
        total_credits_outstanding=total_credits,
    )


async def _get_user_or_404(user_id, db: AsyncSession) -> User:
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post(
    "/users/{user_id}/credits",
    response_model=AdminUserOut,
    dependencies=[Depends(verify_admin_secret)],
)
async def update_credits(user_id: str, payload: AdminCreditsUpdate, db: AsyncSession = Depends(get_db)):
    user = await _get_user_or_404(uuid_module.UUID(user_id), db)
    user.credits = max(0, user.credits + payload.delta)
    await db.commit()
    await db.refresh(user)
    return await _user_to_out(user, db)


@router.post(
    "/users/{user_id}/plan",
    response_model=AdminUserOut,
    dependencies=[Depends(verify_admin_secret)],
)
async def update_plan(user_id: str, payload: AdminPlanUpdate, db: AsyncSession = Depends(get_db)):
    if payload.plan not in ("free", "premium"):
        raise HTTPException(status_code=400, detail="plan must be 'free' or 'premium'")

    user = await _get_user_or_404(uuid_module.UUID(user_id), db)
    user.plan = payload.plan
    if payload.plan == "premium":
        user.premium_until = (
            datetime.now(timezone.utc) + timedelta(days=payload.days) if payload.days else None
        )
    else:
        user.premium_until = None
    await db.commit()
    await db.refresh(user)
    return await _user_to_out(user, db)


@router.post(
    "/users/{user_id}/active",
    response_model=AdminUserOut,
    dependencies=[Depends(verify_admin_secret)],
)
async def update_active(user_id: str, payload: AdminActiveUpdate, db: AsyncSession = Depends(get_db)):
    user = await _get_user_or_404(uuid_module.UUID(user_id), db)
    user.is_active = payload.is_active
    await db.commit()
    await db.refresh(user)
    return await _user_to_out(user, db)


_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1I")


def _generate_code() -> str:
    chunk = lambda: "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
    return f"UM-{chunk()}-{chunk()}"


def _code_to_out(c: RedeemCode) -> RedeemCodeOut:
    return RedeemCodeOut(
        code=c.code,
        credits=c.credits,
        grants_premium=c.grants_premium,
        premium_days=c.premium_days,
        max_uses=c.max_uses,
        times_used=c.times_used,
        is_active=c.is_active,
        created_at=c.created_at.isoformat() if c.created_at else "",
    )


@router.post(
    "/codes",
    response_model=list[RedeemCodeOut],
    dependencies=[Depends(verify_admin_secret)],
)
async def create_codes(payload: RedeemCodeCreate, db: AsyncSession = Depends(get_db)):
    if payload.credits <= 0 and not payload.grants_premium:
        raise HTTPException(status_code=400, detail="باید یا اعتبار یا پرمیوم مشخص بشه.")
    if payload.quantity < 1 or payload.quantity > 500:
        raise HTTPException(status_code=400, detail="quantity باید بین ۱ تا ۵۰۰ باشه.")
    if payload.max_uses < 1:
        raise HTTPException(status_code=400, detail="max_uses باید حداقل ۱ باشه.")
    if payload.premium_days is not None and payload.premium_days < 1:
        raise HTTPException(status_code=400, detail="premium_days باید حداقل ۱ باشه (یا خالی برای دائمی).")

    created: list[RedeemCode] = []
    for _ in range(payload.quantity):
        # به‌ندرت ممکنه تصادفی تکراری دربیاد؛ چند بار تلاش می‌کنیم
        for _attempt in range(5):
            candidate = _generate_code()
            exists = await db.scalar(select(RedeemCode).where(RedeemCode.code == candidate))
            if not exists:
                break
        else:
            raise HTTPException(status_code=500, detail="ساخت کد یکتا شکست خورد، دوباره امتحان کن.")

        code_row = RedeemCode(
            code=candidate,
            credits=payload.credits,
            grants_premium=payload.grants_premium,
            premium_days=payload.premium_days if payload.grants_premium else None,
            max_uses=payload.max_uses,
        )
        db.add(code_row)
        created.append(code_row)

    await db.commit()
    for c in created:
        await db.refresh(c)

    return [_code_to_out(c) for c in created]


@router.get(
    "/codes",
    response_model=list[RedeemCodeOut],
    dependencies=[Depends(verify_admin_secret)],
)
async def list_codes(db: AsyncSession = Depends(get_db)):
    rows = await db.scalars(select(RedeemCode).order_by(RedeemCode.created_at.desc()).limit(300))
    return [_code_to_out(c) for c in rows.all()]


@router.post(
    "/codes/{code}/revoke",
    response_model=RedeemCodeOut,
    dependencies=[Depends(verify_admin_secret)],
)
async def revoke_code(code: str, db: AsyncSession = Depends(get_db)):
    row = await db.scalar(select(RedeemCode).where(RedeemCode.code == code.strip().upper()))
    if not row:
        raise HTTPException(status_code=404, detail="کد پیدا نشد.")
    row.is_active = False
    await db.commit()
    await db.refresh(row)
    return _code_to_out(row)


@router.get("/panel", response_class=HTMLResponse)
async def admin_panel():
    """
    صفحه‌ی مدیریتی. خودِ این صفحه محافظت‌شده نیست (فقط HTML خالیه)،
    ولی هر API که ازش صدا زده می‌شه با هدر X-Admin-Secret چک می‌شه.
    """
    return PANEL_HTML


PANEL_HTML = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>پنل مدیریت UniMate AI</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #171a21;
    --border: #2a2e38;
    --text: #e8e9ec;
    --muted: #9aa0ab;
    --accent: #5b8def;
    --green: #3ecf8e;
    --red: #e5484d;
    --amber: #e5a83d;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, "Segoe UI", Tahoma, Vazirmatn, sans-serif;
    margin: 0;
    padding: 16px;
  }
  h1 { font-size: 20px; margin: 0 0 16px; }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 16px;
  }
  input, select, button {
    font-family: inherit;
    font-size: 14px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: #1d212b;
    color: var(--text);
    padding: 8px 10px;
  }
  button {
    cursor: pointer;
    background: var(--accent);
    border: none;
    color: white;
    font-weight: 600;
  }
  button.secondary { background: #2a2e38; }
  button.danger { background: var(--red); }
  button.success { background: var(--green); }
  button:active { opacity: 0.8; }
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 10px;
  }
  .stat-box {
    background: #1d212b;
    border-radius: 10px;
    padding: 10px;
    text-align: center;
  }
  .stat-box .num { font-size: 20px; font-weight: 700; }
  .stat-box .label { font-size: 11px; color: var(--muted); margin-top: 2px; }
  .search-row { display: flex; gap: 8px; margin-bottom: 12px; }
  .search-row input { flex: 1; }
  .toolbar-row { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }
  .chip {
    font-size: 12px;
    padding: 6px 12px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: #1d212b;
    color: var(--muted);
    cursor: pointer;
  }
  .chip.active { background: var(--accent); color: white; border-color: var(--accent); }
  .sort-select { margin-inline-start: auto; }
  .result-count { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
  .gen-form { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; margin-top: 10px; }
  .gen-label {
    display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted);
  }
  .gen-label input[type="number"] { width: 90px; }
  .checkbox-label { flex-direction: row; align-items: center; gap: 6px; font-size: 13px; color: var(--text); }
  .checkbox-label input { width: auto; }
  .code-card {
    border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px;
    margin-bottom: 8px; background: #1d212b;
    display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;
  }
  .code-value {
    font-family: "Courier New", monospace; font-size: 15px; font-weight: 700;
    letter-spacing: 1px; color: var(--accent);
  }
  .code-meta { font-size: 11px; color: var(--muted); }
  .code-revoked { opacity: 0.5; text-decoration: line-through; }
  .user-card {
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px;
    margin-bottom: 10px;
    background: #1d212b;
  }
  .user-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
  .user-name { font-weight: 600; font-size: 14px; }
  .user-email { color: var(--muted); font-size: 12px; }
  .badge {
    display: inline-block;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 999px;
    margin-inline-start: 6px;
  }
  .badge.premium { background: rgba(62,207,142,0.15); color: var(--green); }
  .badge.free { background: rgba(154,160,171,0.15); color: var(--muted); }
  .badge.inactive { background: rgba(229,72,77,0.15); color: var(--red); }
  .user-meta { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
  .actions-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .credits-input { width: 70px; }
  #loginBox { max-width: 360px; margin: 60px auto; }
  #loginBox input { width: 100%; margin-bottom: 10px; }
  #loginBox button { width: 100%; }
  .hidden { display: none; }
  .error-text { color: var(--red); font-size: 13px; margin-top: 8px; }
  .toast {
    position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
    background: #1d212b; border: 1px solid var(--border); padding: 10px 16px;
    border-radius: 10px; font-size: 13px; opacity: 0; transition: opacity 0.2s;
    pointer-events: none;
  }
  .toast.show { opacity: 1; }
</style>
</head>
<body>

<div id="loginBox" class="card">
  <h1>ورود به پنل مدیریت</h1>
  <input id="secretInput" type="password" placeholder="رمز ادمین (ADMIN_SECRET)">
  <button onclick="login()">ورود</button>
  <div id="loginError" class="error-text hidden"></div>
</div>

<div id="mainBox" class="hidden">
  <h1>پنل مدیریت UniMate AI</h1>

  <div class="card">
    <div id="statsGrid" class="stats-grid"></div>
  </div>

  <div class="card">
    <h1 style="font-size:16px;">🎟️ تولید کد شارژ / پرمیوم</h1>
    <div class="gen-form">
      <label class="gen-label">مقدار اعتبار
        <input id="genCredits" type="number" value="0" min="0">
      </label>
      <label class="gen-label checkbox-label">
        <input id="genPremium" type="checkbox" onchange="toggleGenPremiumDuration()">
        پرمیوم کن
      </label>
      <label class="gen-label" id="genPremiumDurationWrap" style="display:none;">مدت پرمیوم
        <select id="genPremiumDays">
          <option value="30">۱ ماهه</option>
          <option value="180">۶ ماهه</option>
          <option value="">دائمی</option>
        </select>
      </label>
      <label class="gen-label">تعداد دفعات استفاده از هر کد
        <input id="genMaxUses" type="number" value="1" min="1">
      </label>
      <label class="gen-label">تعداد کد (برای ساخت چندتایی)
        <input id="genQuantity" type="number" value="1" min="1" max="500">
      </label>
      <button onclick="generateCodes()">تولید کد</button>
    </div>
    <div id="genResult"></div>
  </div>

  <div class="card">
    <div class="search-row">
      <input id="codeSearchInput" type="text" placeholder="جستجو در کدها..." oninput="renderCodes()">
      <button class="secondary" onclick="loadCodes()">🔄 بروزرسانی کدها</button>
    </div>
    <div id="codesList"></div>
  </div>

  <div class="card">
    <div class="search-row">
      <input id="searchInput" type="text" placeholder="جستجو با ایمیل، اسم یا آیدی تلگرام..." oninput="renderUsers()">
      <button class="secondary" onclick="loadAll()">🔄 بروزرسانی</button>
    </div>
    <div class="toolbar-row" id="filterChips">
      <span class="chip active" data-filter="all" onclick="setFilter('all')">همه</span>
      <span class="chip" data-filter="premium" onclick="setFilter('premium')">پرمیوم</span>
      <span class="chip" data-filter="free" onclick="setFilter('free')">رایگان</span>
      <span class="chip" data-filter="inactive" onclick="setFilter('inactive')">غیرفعال</span>
      <span class="chip" data-filter="low_credits" onclick="setFilter('low_credits')">اعتبار کم (زیر ۵)</span>
      <select id="sortSelect" class="sort-select" onchange="renderUsers()">
        <option value="newest">جدیدترین</option>
        <option value="oldest">قدیمی‌ترین</option>
        <option value="credits_desc">بیشترین اعتبار</option>
        <option value="credits_asc">کمترین اعتبار</option>
        <option value="notes_desc">بیشترین نوت</option>
        <option value="name">اسم (الفبایی)</option>
      </select>
    </div>
    <div class="result-count" id="resultCount"></div>
    <div id="usersList"></div>
  </div>
</div>


<div id="toast" class="toast"></div>

<script>
let ADMIN_SECRET = "";
let ALL_USERS = [];
let ALL_CODES = [];

function showToast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2000);
}

async function apiCall(path, method = "GET", body = null) {
  const opts = {
    method,
    headers: {
      "X-Admin-Secret": ADMIN_SECRET,
      "Content-Type": "application/json",
    },
  };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || "خطا");
  }
  return resp.json();
}

async function login() {
  ADMIN_SECRET = document.getElementById("secretInput").value.trim();
  const errBox = document.getElementById("loginError");
  errBox.classList.add("hidden");
  try {
    await loadAll();
    document.getElementById("loginBox").classList.add("hidden");
    document.getElementById("mainBox").classList.remove("hidden");
    sessionStorage.setItem("unimate_admin_secret", ADMIN_SECRET);
  } catch (e) {
    errBox.textContent = "رمز اشتباهه یا اتصال برقرار نشد.";
    errBox.classList.remove("hidden");
  }
}

async function loadAll() {
  const [stats, users, codes] = await Promise.all([
    apiCall("/admin/stats"),
    apiCall("/admin/users"),
    apiCall("/admin/codes"),
  ]);
  renderStats(stats);
  ALL_USERS = users;
  ALL_CODES = codes;
  renderUsers();
  renderCodes();
}

async function loadCodes() {
  ALL_CODES = await apiCall("/admin/codes");
  renderCodes();
}

function toggleGenPremiumDuration() {
  const checked = document.getElementById("genPremium").checked;
  document.getElementById("genPremiumDurationWrap").style.display = checked ? "flex" : "none";
}

async function generateCodes() {
  const credits = parseInt(document.getElementById("genCredits").value, 10) || 0;
  const grants_premium = document.getElementById("genPremium").checked;
  const premiumDaysRaw = document.getElementById("genPremiumDays").value;
  const premium_days = grants_premium && premiumDaysRaw ? parseInt(premiumDaysRaw, 10) : null;
  const max_uses = parseInt(document.getElementById("genMaxUses").value, 10) || 1;
  const quantity = parseInt(document.getElementById("genQuantity").value, 10) || 1;

  if (credits <= 0 && !grants_premium) {
    showToast("باید یا اعتبار بدی یا پرمیوم رو تیک بزنی");
    return;
  }

  try {
    const newCodes = await apiCall("/admin/codes", "POST", {
      credits, grants_premium, premium_days, max_uses, quantity,
    });
    ALL_CODES = [...newCodes, ...ALL_CODES];
    renderCodes();

    const box = document.getElementById("genResult");
    box.innerHTML =
      '<div class="user-meta" style="margin-top:10px;">کد(های) جدید (برای کپی روشون بزن):</div>' +
      newCodes.map((c) => codeCardHtml(c)).join("");
    showToast(`${newCodes.length} کد ساخته شد`);
  } catch (e) {
    showToast("خطا: " + e.message);
  }
}

function renderCodes() {
  const q = document.getElementById("codeSearchInput").value.trim().toLowerCase();
  const filtered = ALL_CODES.filter((c) => !q || c.code.toLowerCase().includes(q));
  document.getElementById("codesList").innerHTML =
    filtered.map((c) => codeCardHtml(c)).join("") || '<div class="user-meta">کدی پیدا نشد.</div>';
}

function codeCardHtml(c) {
  const perks = [];
  if (c.credits) perks.push(`${c.credits} اعتبار`);
  if (c.grants_premium) {
    perks.push(c.premium_days ? `پرمیوم ${c.premium_days} روزه` : "پرمیوم دائمی");
  }
  const statusClass = c.is_active ? "" : "code-revoked";
  return `
    <div class="code-card">
      <div>
        <div class="code-value ${statusClass}" onclick="copyCode('${c.code}')">${c.code}</div>
        <div class="code-meta">${perks.join(" + ")} · استفاده: ${c.times_used}/${c.max_uses} ${c.is_active ? "" : "· غیرفعال"}</div>
      </div>
      <div class="actions-row">
        <button class="secondary" onclick="copyCode('${c.code}')">📋 کپی</button>
        ${c.is_active ? `<button class="danger" onclick="revokeCode('${c.code}')">ابطال</button>` : ""}
      </div>
    </div>
  `;
}

async function copyCode(code) {
  try {
    await navigator.clipboard.writeText(code);
    showToast("کد کپی شد: " + code);
  } catch (e) {
    showToast(code);
  }
}

async function revokeCode(code) {
  try {
    const updated = await apiCall(`/admin/codes/${code}/revoke`, "POST");
    const idx = ALL_CODES.findIndex((c) => c.code === updated.code);
    if (idx !== -1) ALL_CODES[idx] = updated;
    renderCodes();
    showToast("کد باطل شد");
  } catch (e) {
    showToast("خطا: " + e.message);
  }
}

function renderStats(s) {
  const items = [
    ["کل کاربران", s.total_users],
    ["پرمیوم", s.premium_users],
    ["رایگان", s.free_users],
    ["غیرفعال", s.inactive_users],
    ["کل نوت‌ها", s.total_notes],
    ["مجموع اعتبار", s.total_credits_outstanding],
  ];
  document.getElementById("statsGrid").innerHTML = items
    .map(([label, num]) => `<div class="stat-box"><div class="num">${num}</div><div class="label">${label}</div></div>`)
    .join("");
}

let CURRENT_FILTER = "all";

function setFilter(name) {
  CURRENT_FILTER = name;
  document.querySelectorAll("#filterChips .chip").forEach((el) => {
    el.classList.toggle("active", el.dataset.filter === name);
  });
  renderUsers();
}

function renderUsers() {
  const q = document.getElementById("searchInput").value.trim().toLowerCase();

  let filtered = ALL_USERS.filter((u) => {
    if (!q) return true;
    return (
      u.email.toLowerCase().includes(q) ||
      u.full_name.toLowerCase().includes(q) ||
      (u.telegram_id || "").includes(q)
    );
  });

  if (CURRENT_FILTER === "premium") filtered = filtered.filter((u) => u.plan === "premium");
  else if (CURRENT_FILTER === "free") filtered = filtered.filter((u) => u.plan === "free");
  else if (CURRENT_FILTER === "inactive") filtered = filtered.filter((u) => !u.is_active);
  else if (CURRENT_FILTER === "low_credits") filtered = filtered.filter((u) => u.plan !== "premium" && u.credits < 5);

  const sortBy = document.getElementById("sortSelect").value;
  filtered = [...filtered].sort((a, b) => {
    switch (sortBy) {
      case "oldest": return a.created_at.localeCompare(b.created_at);
      case "credits_desc": return b.credits - a.credits;
      case "credits_asc": return a.credits - b.credits;
      case "notes_desc": return b.notes_count - a.notes_count;
      case "name": return a.full_name.localeCompare(b.full_name);
      default: return b.created_at.localeCompare(a.created_at); // newest
    }
  });

  document.getElementById("resultCount").textContent = `${filtered.length} از ${ALL_USERS.length} کاربر`;
  document.getElementById("usersList").innerHTML = filtered.map((u) => userCardHtml(u)).join("") ||
    '<div class="user-meta">کاربری پیدا نشد.</div>';
}

function userCardHtml(u) {
  const planBadge = u.plan === "premium"
    ? '<span class="badge premium">پرمیوم</span>'
    : '<span class="badge free">رایگان</span>';
  const activeBadge = u.is_active ? "" : '<span class="badge inactive">غیرفعال</span>';
  const tgLine = u.telegram_id ? `آیدی تلگرام: ${u.telegram_id}` : "";
  const createdDate = u.created_at ? u.created_at.split("T")[0] : "";

  let premiumLine = "";
  if (u.plan === "premium") {
    premiumLine = u.premium_until
      ? `تا ${u.premium_until.split("T")[0]}`
      : "دائمی";
  }

  return `
    <div class="user-card">
      <div class="user-top">
        <div>
          <span class="user-name">${escapeHtml(u.full_name)}</span>
          ${planBadge}${activeBadge}
        </div>
      </div>
      <div class="user-email">${escapeHtml(u.email)}</div>
      <div class="user-meta">
        ${tgLine ? tgLine + " · " : ""}اعتبار: ${u.credits} · نوت‌ها: ${u.notes_count} · عضویت: ${createdDate}
        ${premiumLine ? " · پرمیوم " + premiumLine : ""}
      </div>
      <div class="actions-row">
        <input type="number" class="credits-input" id="delta-${u.id}" placeholder="±مقدار" value="10">
        <button onclick="adjustCredits('${u.id}', 1)">➕ شارژ</button>
        <button class="secondary" onclick="adjustCredits('${u.id}', -1)">➖ کسر</button>
        ${
          u.plan === "premium"
            ? `<button class="secondary" onclick="setPlan('${u.id}', 'free')">تبدیل به رایگان</button>`
            : `
              <select id="premiumDays-${u.id}" class="credits-input" style="width:90px;">
                <option value="30">۱ ماهه</option>
                <option value="180">۶ ماهه</option>
                <option value="">دائمی</option>
              </select>
              <button class="success" onclick="setPlan('${u.id}', 'premium')">ارتقا به پرمیوم</button>
            `
        }
        ${
          u.is_active
            ? `<button class="danger" onclick="setActive('${u.id}', false)">غیرفعال کردن</button>`
            : `<button class="success" onclick="setActive('${u.id}', true)">فعال کردن</button>`
        }
      </div>
    </div>
  `;
}

async function adjustCredits(userId, sign) {
  const input = document.getElementById(`delta-${userId}`);
  const amount = parseInt(input.value, 10) || 0;
  if (!amount) return;
  try {
    const updated = await apiCall(`/admin/users/${userId}/credits`, "POST", { delta: sign * amount });
    patchUser(updated);
    showToast(`اعتبار جدید: ${updated.credits}`);
  } catch (e) {
    showToast("خطا: " + e.message);
  }
}

async function setPlan(userId, plan) {
  try {
    let days = null;
    if (plan === "premium") {
      const sel = document.getElementById(`premiumDays-${userId}`);
      days = sel && sel.value ? parseInt(sel.value, 10) : null;
    }
    const updated = await apiCall(`/admin/users/${userId}/plan`, "POST", { plan, days });
    patchUser(updated);
    showToast(plan === "premium" ? "به پرمیوم ارتقا یافت" : "به رایگان تبدیل شد");
  } catch (e) {
    showToast("خطا: " + e.message);
  }
}

async function setActive(userId, isActive) {
  try {
    const updated = await apiCall(`/admin/users/${userId}/active`, "POST", { is_active: isActive });
    patchUser(updated);
    showToast(isActive ? "کاربر فعال شد" : "کاربر غیرفعال شد");
  } catch (e) {
    showToast("خطا: " + e.message);
  }
}

function patchUser(updated) {
  const idx = ALL_USERS.findIndex((u) => u.id === updated.id);
  if (idx !== -1) ALL_USERS[idx] = updated;
  renderUsers();
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// اگه قبلاً لاگین کرده بودی، دوباره رمز نپرس
window.addEventListener("DOMContentLoaded", () => {
  const saved = sessionStorage.getItem("unimate_admin_secret");
  if (saved) {
    ADMIN_SECRET = saved;
    loadAll()
      .then(() => {
        document.getElementById("loginBox").classList.add("hidden");
        document.getElementById("mainBox").classList.remove("hidden");
      })
      .catch(() => {
        sessionStorage.removeItem("unimate_admin_secret");
      });
  }
});
</script>
</body>
</html>
"""
