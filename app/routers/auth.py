"""Auth & organizations: registration, email verify, login, invite links, EGRUL.

Email behaviour: when no RESEND_API_KEY is configured the account is auto-verified
on registration (tokens returned immediately) so the flow works without a mail
provider. With Resend configured, registration returns {verified:false} and the
user must click the emailed link (GET /api/auth/verify-email) to get tokens.
"""

import asyncio
import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.auth import (
    REFRESH_TOKEN_EXPIRE_DAYS,
    create_access_token,
    create_refresh_token,
    get_current_user,
    hash_password,
    verify_password,
    verify_token,
)
from app.categories_seed import seed_default_categories
from app.database import get_pool
from app.email_service import email_enabled, send_verification_email

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/api", tags=["auth"])

APP_URL = os.getenv("APP_URL", "https://aocgaiofficeweb.up.railway.app")


# ─── models ───
class RegisterIn(BaseModel):
    phone: Optional[str] = None
    email: str
    password: str
    first_name: str = ""
    last_name: str = ""
    org_type: str = "company"  # 'person' | 'company'
    org_name: Optional[str] = None
    inn: Optional[str] = None


class LoginIn(BaseModel):
    phone_or_email: str
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class LogoutIn(BaseModel):
    refresh_token: Optional[str] = None


class InviteCreateIn(BaseModel):
    role: str = "employee"
    expires_hours: Optional[int] = None  # None = permanent (no expiry)
    max_uses: int = 1


class RegisterByInviteIn(BaseModel):
    token: str
    phone: Optional[str] = None
    email: str
    password: str
    first_name: str = ""
    last_name: str = ""


# ─── helpers ───
def _public_user(u: dict) -> dict:
    keys = (
        "id",
        "first_name",
        "last_name",
        "patronymic",
        "email",
        "phone",
        "role",
        "org_id",
        "is_email_verified",
        "is_active",
    )
    return {k: u.get(k) for k in keys}


async def _org(p, org_id) -> Optional[dict]:
    if not org_id:
        return None
    row = await p.fetchrow(
        "SELECT id, name, inn, type FROM organizations WHERE id=$1", org_id
    )
    return dict(row) if row else None


def _validate(email: str, password: str):
    if not password or len(password) < 8:
        raise HTTPException(
            status_code=400, detail="Пароль должен быть не менее 8 символов"
        )
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Некорректный email")


async def _auth_payload(p, user_row) -> dict:
    u = dict(user_row)
    return {
        "verified": True,
        "access_token": create_access_token(u["id"]),
        "refresh_token": create_refresh_token(u["id"]),
        "user": _public_user(u),
        "organization": await _org(p, u.get("org_id")),
    }


# ─── registration (admin → new organization) ───
@router.post("/auth/register")
async def register(body: RegisterIn):
    email = body.email.strip().lower()
    _validate(email, body.password)
    p = await get_pool()
    existing = await p.fetchrow("SELECT * FROM users WHERE lower(email)=$1", email)
    if existing:
        if existing["password_hash"]:
            raise HTTPException(
                status_code=409, detail="Этот email уже зарегистрирован"
            )
        # Claim a password-less seeded account (the pre-auth admin): set the
        # password and keep its existing organization & data — don't make a new one.
        auto_verify = not email_enabled()
        verify_tok = None if auto_verify else uuid.uuid4().hex
        row = await p.fetchrow(
            """UPDATE users SET password_hash=$1,
                      phone=COALESCE($2, phone),
                      first_name=COALESCE(NULLIF($3,''), first_name),
                      last_name=COALESCE(NULLIF($4,''), last_name),
                      is_email_verified=$5, email_verify_token=$6
               WHERE id=$7 RETURNING *""",
            hash_password(body.password),
            body.phone,
            body.first_name,
            body.last_name,
            auto_verify,
            verify_tok,
            existing["id"],
        )
        if auto_verify:
            return await _auth_payload(p, row)
        send_verification_email(email, f"{APP_URL}/verify-email?token={verify_tok}")
        return {
            "verified": False,
            "message": "Проверьте email для подтверждения аккаунта",
        }

    org_type = "person" if body.org_type == "person" else "company"
    org_name = (body.org_name or "").strip() or (
        f"{body.first_name} {body.last_name}".strip() or "Личный кабинет"
    )
    auto_verify = not email_enabled()
    verify_tok = None if auto_verify else uuid.uuid4().hex

    async with p.acquire() as conn:
        async with conn.transaction():
            org = await conn.fetchrow(
                "INSERT INTO organizations (name, inn, type) VALUES ($1,$2,$3) RETURNING id",
                org_name,
                body.inn,
                org_type,
            )
            user = await conn.fetchrow(
                """INSERT INTO users (first_name,last_name,email,phone,password_hash,role,org_id,
                                      is_email_verified,email_verify_token)
                   VALUES ($1,$2,$3,$4,$5,'admin',$6,$7,$8) RETURNING *""",
                body.first_name,
                body.last_name,
                email,
                body.phone,
                hash_password(body.password),
                org["id"],
                auto_verify,
                verify_tok,
            )
            await conn.execute(
                "UPDATE organizations SET owner_id=$1 WHERE id=$2",
                user["id"],
                org["id"],
            )
            # Фикс №1 фаза A: новая орг сразу получает дефолтный справочник
            # (11 групп + 48 статей) — в той же транзакции, что и создание орг.
            await seed_default_categories(conn, org["id"])

    if auto_verify:
        return await _auth_payload(p, user)
    send_verification_email(email, f"{APP_URL}/verify-email?token={verify_tok}")
    return {"verified": False, "message": "Проверьте email для подтверждения аккаунта"}


@router.get("/auth/verify-email")
async def verify_email(token: str):
    p = await get_pool()
    row = await p.fetchrow("SELECT * FROM users WHERE email_verify_token=$1", token)
    if not row:
        raise HTTPException(
            status_code=400, detail="Ссылка недействительна или истекла"
        )
    await p.execute(
        "UPDATE users SET is_email_verified=true, email_verify_token=NULL WHERE id=$1",
        row["id"],
    )
    return await _auth_payload(p, row)


@router.post("/auth/login")
@limiter.limit("10/minute")
async def login(request: Request, body: LoginIn):
    ident = body.phone_or_email.strip()
    p = await get_pool()
    row = await p.fetchrow(
        "SELECT * FROM users WHERE lower(email)=lower($1) OR phone=$1", ident
    )
    if not row:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    u = dict(row)
    now = datetime.now(timezone.utc)

    if u.get("locked_until") and u["locked_until"] > now:
        mins = int((u["locked_until"] - now).total_seconds() // 60) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много попыток. Попробуйте через {mins} мин",
        )

    if not verify_password(body.password, u.get("password_hash")):
        attempts = (u.get("failed_attempts") or 0) + 1
        locked = now + timedelta(minutes=15) if attempts >= 5 else None
        await p.execute(
            "UPDATE users SET failed_attempts=$1, locked_until=$2 WHERE id=$3",
            attempts,
            locked,
            u["id"],
        )
        if locked:
            raise HTTPException(
                status_code=429, detail="Слишком много попыток. Попробуйте через 15 мин"
            )
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    if not u.get("is_email_verified"):
        raise HTTPException(status_code=403, detail="Подтвердите email")

    await p.execute(
        "UPDATE users SET failed_attempts=0, locked_until=NULL, last_login_at=NOW() WHERE id=$1",
        u["id"],
    )
    return await _auth_payload(p, u)


@router.post("/auth/refresh")
async def refresh(body: RefreshIn):
    uid = verify_token(body.refresh_token, "refresh")
    if uid is None:
        raise HTTPException(status_code=401, detail="Сессия истекла")
    p = await get_pool()
    th = hashlib.sha256(body.refresh_token.encode()).hexdigest()
    if await p.fetchrow("SELECT 1 FROM revoked_tokens WHERE token_hash=$1", th):
        raise HTTPException(status_code=401, detail="Сессия истекла")
    if not await p.fetchrow("SELECT 1 FROM users WHERE id=$1 AND is_active=true", uid):
        raise HTTPException(status_code=401, detail="Сессия истекла")
    return {"access_token": create_access_token(uid)}


@router.post("/auth/logout")
async def logout(body: LogoutIn):
    if body.refresh_token:
        p = await get_pool()
        th = hashlib.sha256(body.refresh_token.encode()).hexdigest()
        exp = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
        await p.execute(
            "INSERT INTO revoked_tokens (token_hash, expires_at) VALUES ($1,$2)",
            th,
            exp,
        )
    return {"ok": True}


@router.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    p = await get_pool()
    return {
        "user": _public_user(user),
        "organization": await _org(p, user.get("org_id")),
    }


# ─── invites ───
def _require_admin(user: dict):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Только для администратора")


@router.post("/invite/create")
async def invite_create(body: InviteCreateIn, user: dict = Depends(get_current_user)):
    _require_admin(user)
    token = secrets.token_urlsafe(32)
    expires = (
        None
        if body.expires_hours is None
        else datetime.now(timezone.utc) + timedelta(hours=body.expires_hours)
    )
    p = await get_pool()
    row = await p.fetchrow(
        """INSERT INTO invite_links (token, org_id, role, created_by, expires_at, max_uses)
           VALUES ($1,$2,$3,$4,$5,$6) RETURNING *""",
        token,
        user["org_id"],
        body.role,
        user["id"],
        expires,
        body.max_uses,
    )
    return {
        "token": token,
        "invite_url": f"{APP_URL}/join/{token}",
        "role": body.role,
        "max_uses": body.max_uses,
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
    }


@router.get("/invite/validate/{token}")
async def invite_validate(token: str):
    p = await get_pool()
    row = await p.fetchrow("SELECT * FROM invite_links WHERE token=$1", token)
    now = datetime.now(timezone.utc)
    valid = bool(
        row
        and row["is_active"]
        and (row["expires_at"] is None or row["expires_at"] > now)
        and row["uses_count"] < row["max_uses"]
    )
    org = await _org(p, row["org_id"]) if row else None
    return {
        "is_valid": valid,
        "role": row["role"] if row else None,
        "org_name": org["name"] if org else None,
        "expires_at": row["expires_at"].isoformat()
        if row and row["expires_at"]
        else None,
    }


@router.post("/auth/register-by-invite")
async def register_by_invite(body: RegisterByInviteIn):
    email = body.email.strip().lower()
    _validate(email, body.password)
    p = await get_pool()
    inv = await p.fetchrow("SELECT * FROM invite_links WHERE token=$1", body.token)
    now = datetime.now(timezone.utc)
    if not (
        inv
        and inv["is_active"]
        and (inv["expires_at"] is None or inv["expires_at"] > now)
        and inv["uses_count"] < inv["max_uses"]
    ):
        raise HTTPException(
            status_code=400, detail="Ссылка недействительна или истекла"
        )
    if await p.fetchrow("SELECT id FROM users WHERE lower(email)=$1", email):
        raise HTTPException(status_code=409, detail="Этот email уже зарегистрирован")

    auto_verify = not email_enabled()
    verify_tok = None if auto_verify else uuid.uuid4().hex
    async with p.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                """INSERT INTO users (first_name,last_name,email,phone,password_hash,role,org_id,
                                      is_email_verified,email_verify_token)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *""",
                body.first_name,
                body.last_name,
                email,
                body.phone,
                hash_password(body.password),
                inv["role"],
                inv["org_id"],
                auto_verify,
                verify_tok,
            )
            new_uses = inv["uses_count"] + 1
            await conn.execute(
                "UPDATE invite_links SET uses_count=$1, is_active=$2 WHERE id=$3",
                new_uses,
                new_uses < inv["max_uses"],
                inv["id"],
            )

    if auto_verify:
        return await _auth_payload(p, user)
    send_verification_email(email, f"{APP_URL}/verify-email?token={verify_tok}")
    return {"verified": False, "message": "Проверьте email"}


@router.get("/invite/list")
async def invite_list(user: dict = Depends(get_current_user)):
    _require_admin(user)
    p = await get_pool()
    rows = await p.fetch(
        "SELECT * FROM invite_links WHERE org_id=$1 AND is_active=true ORDER BY created_at DESC",
        user["org_id"],
    )
    return [
        {
            "token": r["token"],
            "invite_url": f"{APP_URL}/join/{r['token']}",
            "role": r["role"],
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
            "max_uses": r["max_uses"],
            "uses_count": r["uses_count"],
        }
        for r in rows
    ]


@router.delete("/invite/{token}")
async def invite_delete(token: str, user: dict = Depends(get_current_user)):
    _require_admin(user)
    p = await get_pool()
    await p.execute(
        "UPDATE invite_links SET is_active=false WHERE token=$1 AND org_id=$2",
        token,
        user["org_id"],
    )
    return {"ok": True}


# ─── EGRUL lookup by ИНН (best-effort; falls back to null) ───
@router.get("/egrul/{inn}")
async def egrul(inn: str):
    digits = "".join(ch for ch in inn if ch.isdigit())
    if len(digits) not in (10, 12):
        return None
    # egrul.nalog.ru is a 2-step token+poll flow and often blocks server-side
    # calls. Try once; on any failure return null so the client uses manual entry.
    # (A reliable lookup would use a paid/keyed service such as DaData.)
    try:
        async with httpx.AsyncClient(
            timeout=8.0, headers={"User-Agent": "Mozilla/5.0"}
        ) as client:
            init = await client.post(
                "https://egrul.nalog.ru/", data={"query": digits, "page": ""}
            )
            tok = init.json().get("t")
            if not tok:
                return None
            for _ in range(5):
                res = await client.get(f"https://egrul.nalog.ru/search-result/{tok}")
                data = res.json()
                if data.get("status") == "wait":
                    await asyncio.sleep(0.8)
                    continue
                rows = data.get("rows") or []
                if rows:
                    item = rows[0]
                    return {
                        "name": item.get("c") or item.get("n"),
                        "inn": item.get("i") or digits,
                        "ogrn": item.get("o"),
                    }
                break
    except Exception as e:  # noqa: BLE001
        print(f"[EGRUL] {type(e).__name__}: {e}", flush=True)
    return None
