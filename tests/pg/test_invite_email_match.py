# -*- coding: utf-8 -*-
"""Именная ссылка сверяет почту — НА ЖИВОЙ БАЗЕ (T168, этап 2).

⚠️ ЗАЧЕМ, ЗАМЕРОМ ПРОДА 04.09.2026 (бастион). Приглашение №8 звало на
a.shu@aocg.ru, человек ввёл ровно этот адрес — и всё сошлось. Но совпал
ЧЕЛОВЕК, а не система: `inv["email"]` в register_by_invite не читался ни разу,
и по именной ссылке мог зарегистрироваться кто угодно с любой почтой.

⚠️ ПОРЯДОК ПРОВЕРОК — ЧАСТЬ ЗАЩИТЫ, А НЕ СТИЛЬ. Сверка адреса стоит ДО проверки
занятости почты. Стой она после — на чужом адресе человек сначала получал бы
«этот email уже зарегистрирован», и приглашение работало бы проверялкой чужих
почт для любого, кому ссылка попала в руки. Это отдельный тест ниже.

⚠️ ЧИТАЕМ СОСТОЯНИЕ БАЗЫ, А НЕ КОД ОТВЕТА: 400 можно вернуть и всё равно
создать пользователя (разрыв ответа и состояния — T166). Поэтому после каждой
попытки смотрим, есть ли строка в `users` и сдвинулся ли `uses_count`.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.routers import auth as auth_router

ADMIN_ID = 1
ORG = 1
ЗАВТРА = datetime.now(timezone.utc) + timedelta(hours=24)


@pytest_asyncio.fixture
async def орг(db, monkeypatch):
    """Организация с админом; почта выключена, чтобы регистрация сразу
    возвращала токены, а не уходила в ветку подтверждения адреса."""
    await db.добавить_организацию(ORG, "АОЦГ")
    await db.добавить_пользователя(id=ADMIN_ID, first_name="Админ", role="admin")
    monkeypatch.setattr(auth_router, "email_enabled", lambda: False)
    return db


async def _ссылка(db, token, *, email, role="employee"):
    await db.pool.execute(
        """INSERT INTO invite_links
             (token, org_id, role, created_by, expires_at, max_uses, email)
           VALUES ($1,$2,$3,$4,$5,1,$6)""",
        token,
        ORG,
        role,
        ADMIN_ID,
        ЗАВТРА,
        email,
    )


async def _пользователь(db, email):
    строка = await db.pool.fetchrow(
        "SELECT * FROM users WHERE lower(email)=$1", email.lower()
    )
    return dict(строка) if строка else None


async def _счётчик(db, token):
    return await db.pool.fetchval(
        "SELECT uses_count FROM invite_links WHERE token=$1", token
    )


# ── 6: свой адрес проходит ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_именная_свой_адрес_регистрирует(db, орг, client):
    await _ссылка(db, "имен", email="ivan@example.com", role="accountant")
    r = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "имен",
            "email": "ivan@example.com",
            "password": "парольдлинный",
            "first_name": "Иван",
        },
    )
    assert r.status_code == 200, r.text
    новый = await _пользователь(db, "ivan@example.com")
    assert новый is not None
    assert новый["org_id"] == ORG and новый["role"] == "accountant"
    assert await _счётчик(db, "имен") == 1


# ── 7: чужой адрес не проходит И НЕ ЖЖЁТ ССЫЛКУ ─────────────────────────────
@pytest.mark.asyncio
async def test_именная_чужой_адрес_отказ_без_создания_и_без_сжигания(db, орг, client):
    await _ссылка(db, "имен", email="ivan@example.com")
    r = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "имен",
            "email": "chuzhoy@example.com",
            "password": "парольдлинный",
        },
    )
    assert r.status_code == 400
    assert await _пользователь(db, "chuzhoy@example.com") is None
    # ⚠️ ЧУЖАЯ ПОПЫТКА НЕ ЖЖЁТ ПРИГЛАШЕНИЕ. Иначе достаточно один раз
    # ошибиться адресом — и звать человека надо заново, новой ссылкой.
    assert await _счётчик(db, "имен") == 0
    строка = await db.pool.fetchrow(
        "SELECT is_active FROM invite_links WHERE token=$1", "имен"
    )
    assert строка["is_active"] is True

    # И сразу следом — верный адрес проходит по ТОЙ ЖЕ ссылке.
    ок = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "имен",
            "email": "ivan@example.com",
            "password": "парольдлинный",
        },
    )
    assert ок.status_code == 200, ок.text
    assert await _пользователь(db, "ivan@example.com") is not None


# ── 11 и «не проверялка чужих почт» ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_отказ_не_называет_верный_адрес(db, орг, client):
    await _ссылка(db, "имен", email="ivan@example.com")
    r = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "имен",
            "email": "chuzhoy@example.com",
            "password": "парольдлинный",
        },
    )
    текст = r.json()["detail"]
    assert "ivan@example.com" not in текст, "верный адрес выдавать нельзя"
    assert "другой адрес" in текст


@pytest.mark.asyncio
async def test_ссылка_не_работает_проверялкой_чужих_почт(db, орг, client):
    """⚠️ ГЛАВНОЕ ПРО ПОРЯДОК ПРОВЕРОК. Держатель ссылки подставляет ЧУЖОЙ
    адрес, который в системе УЖЕ ЗАРЕГИСТРИРОВАН. Ответ обязан быть тем же,
    что и на любой другой чужой адрес: «выписано на другой адрес». Ответ
    «этот email уже зарегистрирован» сказал бы, что такой человек у нас есть.
    """
    await db.добавить_пользователя(
        id=7, first_name="Пётр", email="petr@example.com", role="employee"
    )
    await _ссылка(db, "имен", email="ivan@example.com")

    чужой_известный = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "имен",
            "email": "petr@example.com",
            "password": "парольдлинный",
        },
    )
    чужой_неизвестный = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "имен",
            "email": "nikto@example.com",
            "password": "парольдлинный",
        },
    )
    assert чужой_известный.status_code == чужой_неизвестный.status_code == 400
    assert чужой_известный.json()["detail"] == чужой_неизвестный.json()["detail"], (
        "по ответу видно, есть ли такой человек в системе"
    )


# ── 8: регистр и пробелы ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_регистр_и_пробелы_не_мешают(db, орг, client):
    await _ссылка(db, "имен", email="a.shu@aocg.ru")
    r = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "имен",
            "email": "  A.Shu@AOCG.RU  ",
            "password": "парольдлинный",
        },
    )
    assert r.status_code == 200, r.text
    assert await _пользователь(db, "a.shu@aocg.ru") is not None


# ── 9: общая ссылка сверку не проходит ──────────────────────────────────────
@pytest.mark.asyncio
async def test_общая_ссылка_принимает_любую_почту(db, орг, client):
    await _ссылка(db, "obshaya", email=None)
    r = await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": "obshaya",
            "email": "kto-ugodno@example.com",
            "password": "парольдлинный",
        },
    )
    assert r.status_code == 200, r.text
    новый = await _пользователь(db, "kto-ugodno@example.com")
    assert новый is not None and новый["role"] == "employee"


# ── 10: validate отдаёт адрес и признак ─────────────────────────────────────
@pytest.mark.asyncio
async def test_validate_именной_отдаёт_адрес_и_признак(db, орг, client):
    await _ссылка(db, "имен", email="ivan@example.com", role="accountant")
    d = (await client.get("/api/invite/validate/имен")).json()
    assert d["is_valid"] is True
    assert d["is_personal"] is True
    assert d["email"] == "ivan@example.com"
    assert d["role"] == "accountant" and d["org_name"] == "АОЦГ"


@pytest.mark.asyncio
async def test_validate_общей_отдаёт_признак_и_пустой_адрес(db, орг, client):
    await _ссылка(db, "obshaya", email=None)
    d = (await client.get("/api/invite/validate/obshaya")).json()
    assert d["is_valid"] is True
    assert d["is_personal"] is False
    assert d["email"] is None


@pytest.mark.asyncio
async def test_validate_неизвестного_токена_не_выдаёт_ничего(db, орг, client):
    """Признак и адрес не должны стать новой щелью: по выдуманному токену
    по-прежнему не видно ни организации, ни роли, ни почты."""
    d = (await client.get("/api/invite/validate/выдуманный")).json()
    assert d["is_valid"] is False
    assert d["email"] is None and d["is_personal"] is False
    assert d["role"] is None and d["org_name"] is None
