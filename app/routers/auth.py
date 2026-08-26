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
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.auth import (
    REFRESH_TOKEN_EXPIRE_DAYS,
    ROLE_EMPLOYEE,
    ROLES,
    Role,
    create_access_token,
    create_refresh_token,
    get_current_user,
    hash_password,
    verify_password,
    verify_token,
)
from app.categories_seed import seed_default_categories
from app.database import get_pool
from app.email_service import (
    email_enabled,
    send_password_reset_email,
    send_verification_email,
)

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/api", tags=["auth"])

# APP_URL — адрес ФРОНТА, не API: из него строятся ссылки подтверждения почты
# и приглашений (:166, :211, :397, :478, :493).
#
# УМОЛЧАНИЯ НЕТ, И ЭТО ГЛАВНОЕ В ЭТОЙ СТРОКЕ. До 25.08.2026 здесь стоял
# https://aocgaiofficeweb.up.railway.app — адрес, мёртвый с переезда (S-06).
# Не задана переменная — письмо всё равно уходило, а ссылка внутри вела
# в никуда, и человек видел не «сервис не настроен», а «ссылка не открывается»,
# то есть жаловался на почту, а не на настройку (T62).
#
# ⚠️ ЗАВЕРШАЮЩАЯ КОСАЯ СНИМАЕТСЯ ЗДЕСЬ, А НЕ В МЕСТЕ СКЛЕЙКИ: ссылки строятся
# в пяти местах, и добавлять .rstrip("/") в каждое — значит однажды забыть
# в шестом. Так же сделано у MAX_API (max_relay.py:43).
APP_URL = os.getenv("APP_URL", "").rstrip("/")


# ⚠️ ПИСЬМА УХОДЯТ ЧЕРЕЗ background.add_task, А НЕ ПРЯМЫМ ВЫЗОВОМ (T66).
# Отправка синхронная (smtplib), а обработчики — async. Прямой вызов
# останавливал НЕ запрос, а весь цикл событий: uvicorn поднят одним
# процессом без --workers, то есть цикл на приложение один. Замер 26.08.2026:
# при отправке, длящейся 2 с, посторонний GET / начинался не на 0.20 с,
# как задумано, а на 2.27 — он ждал ЧУЖОГО письма. На проде ожидание не 2 с,
# а 20 (таймаут SMTP).
#
# ПОЧЕМУ ИМЕННО add_task, А НЕ asyncio.create_task: starlette исполняет
# НЕасинхронную задачу через run_in_threadpool (background.py:29), то есть
# разом снимает обе беды — ответ не ждёт письма И цикл не блокируется.
# create_task «выстрелил и забыл» дал бы третью: исключение внутри улетело
# бы в пустоту молча, и мы ослепли бы там, где сейчас видим [EMAIL] send failed.


def проверить_адрес_фронта() -> None:
    """Отказ старта, если APP_URL не задан или задан без схемы (T62).

    ЗОВЁТСЯ ИЗ lifespan (app/main.py), А НЕ ПРИ ИМПОРТЕ — в отличие от
    JWT_SECRET_KEY (app/auth.py:23), который падает прямо на импорте.
    Разница не в строгости, а в том, ЧТО ИМЕННО опасно. У JWT опасен САМ
    ИМПОРТ: модуль с угадываемым секретом уже готов подписывать токены,
    и любой, кто его втянул, получает эту способность. Здесь опасна только
    ОТПРАВКА письма, а импорт писем не шлёт. Отказ на импорте сломал бы
    четыре инструмента замера и шесть документированных команд запуска,
    не добавив к защите ничего.

    ⚠️ ПРОВЕРЯЕТСЯ КОНСТАНТА, А НЕ os.getenv. Перечитать окружение здесь —
    значит проверить не то значение, которое попадёт в письма: константа
    прочитана на импорте, и разойтись они могут (например, если переменную
    выставили между импортом и стартом). Проверяем ровно то, что уйдёт.
    """
    if not APP_URL:
        raise RuntimeError(
            "APP_URL не задан — отказ запуска. Из него строятся ссылки "
            "подтверждения почты и приглашений: без него письмо уйдёт, "
            "а ссылка внутри будет вести в никуда. "
            "Задайте APP_URL, например https://app.aocgai.ru"
        )
    if not APP_URL.startswith(("http://", "https://")):
        raise RuntimeError(
            f"APP_URL={APP_URL!r} — отказ запуска: нет схемы. "
            "Ссылка вида app.aocgai.ru/verify-email не откроется как адрес, "
            "а выглядеть в письме будет правдоподобно. "
            "Нужно https://app.aocgai.ru"
        )


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
    # S-24: белый список ролей (Literal) — без него в invite_links попадала
    # произвольная строка, а из неё в users.role при регистрации по ссылке.
    role: Role = "employee"
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
async def register(body: RegisterIn, background: BackgroundTasks):
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
        background.add_task(
            send_verification_email, email, f"{APP_URL}/verify-email?token={verify_tok}"
        )
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
    background.add_task(
        send_verification_email, email, f"{APP_URL}/verify-email?token={verify_tok}"
    )
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


# ─── восстановление пароля (S-56) ───
#
# ⚠️ ССЫЛКА ИЗ ПИСЬМА — КЛЮЧ НА ПРЕДЪЯВИТЕЛЯ, ЖИВУЩИЙ В ПОЧТОВОМ ЯЩИКЕ.
# Отсюда всё устройство ниже: короткий срок, одноразовость, гашение прежних
# ссылок и всех выданных токенов, проверка is_active В МОМЕНТ ПРИМЕНЕНИЯ.
#
# ⚠️ И ГЛАВНОЕ, ЧТО ЛЕГКО ПОТЕРЯТЬ ПРИ ПРАВКЕ: ОТВЕТ НА СУЩЕСТВУЮЩИЙ
# И НЕСУЩЕСТВУЮЩИЙ АДРЕС ОБЯЗАН БЫТЬ НЕОТЛИЧИМ. Ручка восстановления —
# самое удобное место узнать, кто у нас зарегистрирован: разные ответы
# выдали бы список клиентов. Одинаково должно быть всё: тело, код и —
# насколько мы можем — время.
ЖИЗНЬ_ССЫЛКИ_МИНУТ = 60
ПИСЕМ_НА_АДРЕС_В_ЧАС = 3
ЗАПРОСОВ_С_СЕТЕВОГО_АДРЕСА_В_ЧАС = 20
ОТВЕТ_ВОССТАНОВЛЕНИЯ = {
    "message": "Если такой адрес у нас есть, письмо со ссылкой отправлено"
}


def _хеш_почты(адрес: str) -> str:
    """Адрес приводится к нижнему регистру: ИВАН@ и иван@ — один человек."""
    return hashlib.sha256(адрес.strip().lower().encode()).hexdigest()


def _хеш_токена(токен: str) -> str:
    """⚠️ ТОКЕН ХЕШИРУЕТСЯ КАК ЕСТЬ, БЕЗ strip И БЕЗ lower, И ЭТО НЕ ПРИДИРКА.

    Сначала здесь стояла одна функция на оба случая — та, что приводит
    к нижнему регистру. Для почты это правильно, для токена — потеря стойкости:
    `secrets.token_urlsafe` берёт символы из алфавита с обоими регистрами,
    и приведение к нижнему складывает разные токены в один хеш, вырезая
    примерно по биту на каждую букву. Работало бы всё так же — совпадение
    при проверке достигается, — и именно поэтому ошибка прожила бы долго.
    Поймано тестом, сверяющим хранимое значение с sha256 от токена КАК ОН ЕСТЬ.
    """
    return hashlib.sha256(токен.encode()).hexdigest()


class ForgotIn(BaseModel):
    email: str


class ResetIn(BaseModel):
    token: str
    new_password: str


@router.post("/auth/forgot-password")
@limiter.limit(f"{ЗАПРОСОВ_С_СЕТЕВОГО_АДРЕСА_В_ЧАС}/hour")
async def forgot_password(
    request: Request, body: ForgotIn, background: BackgroundTasks
):
    email = body.email.strip().lower()
    почта_хеш = _хеш_почты(email)
    p = await get_pool()

    # ⚠️ ПОПЫТКИ СЧИТАЮТСЯ ДО ПРОВЕРКИ СУЩЕСТВОВАНИЯ И НЕЗАВИСИМО ОТ НЕЁ.
    # Считай мы только зарегистрированные адреса, пороги оказались бы разными
    # (3 против 20), и перебирающий узнавал бы наши адреса по тому, где лимит
    # наступает раньше: ответ одинаков, а поведение различается.
    await p.execute(
        "DELETE FROM reset_attempts WHERE created_at < NOW() - INTERVAL '1 hour'"
    )
    await p.execute("INSERT INTO reset_attempts (email_hash) VALUES ($1)", почта_хеш)
    за_час = await p.fetchval(
        "SELECT COUNT(*) FROM reset_attempts WHERE email_hash=$1 "
        "AND created_at > NOW() - INTERVAL '1 hour'",
        почта_хеш,
    )
    if за_час and за_час > ПИСЕМ_НА_АДРЕС_В_ЧАС:
        return ОТВЕТ_ВОССТАНОВЛЕНИЯ

    row = await p.fetchrow(
        "SELECT id FROM users WHERE lower(email)=lower($1) AND is_active=true", email
    )
    if row:
        # Прежние невостребованные ссылки гасим: иначе десять запросов дают
        # десять живых ключей от одной учётной записи.
        await p.execute(
            "UPDATE password_resets SET used_at=NOW() "
            "WHERE user_id=$1 AND used_at IS NULL",
            row["id"],
        )
        токен = secrets.token_urlsafe(32)
        await p.execute(
            "INSERT INTO password_resets (user_id, token_hash, expires_at) "
            "VALUES ($1,$2,$3)",
            row["id"],
            _хеш_токена(токен),
            datetime.now(timezone.utc) + timedelta(minutes=ЖИЗНЬ_ССЫЛКИ_МИНУТ),
        )
        background.add_task(
            send_password_reset_email, email, f"{APP_URL}/reset-password?token={токен}"
        )
    return ОТВЕТ_ВОССТАНОВЛЕНИЯ


@router.post("/auth/reset-password")
async def reset_password(body: ResetIn):
    # ⚠️ ОДИН И ТОТ ЖЕ ОТКАЗ НА ВСЕ СЛУЧАИ: нет такого токена, использован,
    # истёк, учётная запись отключена. Разные тексты различали бы состояния
    # чужих ссылок — то есть подсказывали бы перебирающему, что он угадал.
    отказ = HTTPException(status_code=400, detail="Ссылка недействительна")
    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(
            status_code=400, detail="Новый пароль должен быть не менее 8 символов"
        )
    p = await get_pool()
    строка = await p.fetchrow(
        "SELECT id, user_id, expires_at, used_at FROM password_resets "
        "WHERE token_hash=$1",
        _хеш_токена(body.token),
    )
    if (
        not строка
        or строка["used_at"]
        or строка["expires_at"] <= datetime.now(timezone.utc)
    ):
        raise отказ
    # ⚠️ is_active ПРОВЕРЯЕТСЯ ЗДЕСЬ, А НЕ ТОЛЬКО ПРИ ВЫДАЧЕ: удаления
    # пользователя в проекте нет, оно мягкое (users.py, is_active=false).
    # Значит у уволенного сотрудника строка жива, почта та же, и запрошенная
    # до увольнения ссылка осталась бы ключом от учётной записи.
    пользователь = await p.fetchrow(
        "SELECT id FROM users WHERE id=$1 AND is_active=true", строка["user_id"]
    )
    if not пользователь:
        raise отказ

    # Момент из ПРИЛОЖЕНИЯ, а не NOW() базы, и до выдачи новых токенов —
    # разбор в users.py:change_password (S-16).
    момент = datetime.now(timezone.utc)
    await p.execute(
        "UPDATE users SET password_hash=$1, tokens_valid_from=$2 WHERE id=$3",
        hash_password(body.new_password),
        момент,
        строка["user_id"],
    )
    await p.execute(
        "UPDATE password_resets SET used_at=$1 WHERE user_id=$2 AND used_at IS NULL",
        момент,
        строка["user_id"],
    )
    return {"message": "Пароль изменён"}


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

    # ⚠️ БЛОКИРОВКА ИСТЕКЛА — ЧИСТИМ СЧЁТЧИК ДО ПРОВЕРКИ ПАРОЛЯ (S-59).
    # Сюда попадаем, только если проверка выше не сработала, то есть
    # locked_until в прошлом. До 20.08.2026 время снимало ЗАПРЕТ, но не ВИНУ:
    # failed_attempts оставался 5, 10, сколько накопилось, и любая следующая
    # опечатка снова давала attempts >= 5 → мгновенные новые 15 минут.
    # Обнулить мог только успешный вход — а если пароль не вспомнить и
    # восстановления нет (S-56), выхода из петли не существовало вовсе.
    # Живой случай 18.08.2026: у владельца 10 попыток, вход невозможен часами,
    # лечили ручным UPDATE через бастион.
    # Смысл правки: отсидел 15 минут — прощён.
    if u.get("locked_until"):
        await p.execute(
            "UPDATE users SET failed_attempts=0, locked_until=NULL WHERE id=$1",
            u["id"],
        )
        u["failed_attempts"] = 0
        u["locked_until"] = None

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

    # ⚠️ СБРОС ИДЁТ ДО ВОРОТ ПОДТВЕРЖДЕНИЯ ПОЧТЫ (S-59). Счётчик защищает
    # от ПОДБОРА, а верный пароль доказывает, что подбора нет. До 20.08.2026
    # 403 вылетал раньше сброса, и человек с неподтверждённой почтой копил
    # попытки при КАЖДОМ верном вводе.
    await p.execute(
        "UPDATE users SET failed_attempts=0, locked_until=NULL WHERE id=$1",
        u["id"],
    )

    if not u.get("is_email_verified"):
        raise HTTPException(status_code=403, detail="Подтвердите email")

    await p.execute("UPDATE users SET last_login_at=NOW() WHERE id=$1", u["id"])
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
        # ЧИСТКА ЗДЕСЬ, А НЕ ПО РАСПИСАНИЮ (S-16). Колонка `expires_at`
        # заведена под неё с самого начала, но удаления не было нигде —
        # таблица росла навсегда. Планировщика у нас нет, и заводить его
        # ради одной строки значит завести механизм, который некому вызвать:
        # ровно та «ненаписанная чистка, только с кодом». Здесь же чистка
        # привязана к ДЕЙСТВИЮ, которое таблицу и растит, — забыть её
        # невозможно, и стоит она долей миллисекунды на выходе из системы.
        # Просроченная запись бесполезна: сам токен к тому времени истёк
        # и `verify_token` отвергнет его без всякого списка.
        await p.execute("DELETE FROM revoked_tokens WHERE expires_at < NOW()")
    return {"ok": True}


@router.post("/auth/logout-all")
async def logout_all(user: dict = Depends(get_current_user)):
    """«Выйти на всех устройствах» — гасит ВСЕ токены человека разом (S-16).

    Отметкой `tokens_valid_from`, а не чёрным списком: список пришлось бы
    читать на каждом запросе (второй поход в базу) и чистить, а отметка
    приезжает в строке пользователя, которую `get_current_user` и так тянет.

    ТЕКУЩИЙ ТОКЕН ВЫЗЫВАЮЩЕГО ТОЖЕ ПЕРЕСТАЁТ ДЕЙСТВОВАТЬ — это и есть
    смысл ручки: «выйти везде» включает и то устройство, с которого нажали.

    Время берём из ПРИЛОЖЕНИЯ, а не `NOW()` базы: токены подписываются
    часами приложения, и при расхождении часов свежий токен мог бы
    оказаться «старше» отметки и отвергаться сразу после выдачи.
    """
    p = await get_pool()
    момент = datetime.now(timezone.utc)
    await p.execute(
        "UPDATE users SET tokens_valid_from=$1 WHERE id=$2", момент, user["id"]
    )
    return {"ok": True, "отозвано_с": момент}


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
async def register_by_invite(body: RegisterByInviteIn, background: BackgroundTasks):
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
                # S-24: белый список действует и НА ПРИЁМЕ. Валидация модели
                # закрывает только новые приглашения; в базе уже могут лежать
                # выданные раньше, с произвольной ролью. Неизвестную роль
                # понижаем до employee — наименьшие права, а не отказ: человек
                # по ссылке не виноват, что администратор выдал ерунду.
                inv["role"] if inv["role"] in ROLES else ROLE_EMPLOYEE,
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
    background.add_task(
        send_verification_email, email, f"{APP_URL}/verify-email?token={verify_tok}"
    )
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
