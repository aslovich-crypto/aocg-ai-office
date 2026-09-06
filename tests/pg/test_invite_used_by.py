# -*- coding: utf-8 -*-
"""«Кого завели по этой ссылке» — НА ЖИВОЙ БАЗЕ (T168, этап 4).

⚠️ ЗАЧЕМ. Связи «кто по какой ссылке вошёл» в базе не было ВОВСЕ: приглашение
знало, кому его выписали, но не знало, кто им воспользовался. Восстановить
задним числом нечем, и замер владельца 04.09.2026 показал почему дословно —
приглашение №6 звало al@aocg.ru, а сопоставление ПО ВРЕМЕНИ подставляло к нему
Татьяну. Догадка в базе хуже пустоты: пустоту видно, догадку нет.

⚠️ ОТМЕТКА И ПОЛЬЗОВАТЕЛЬ — ОДИН ФАКТ. Запись идёт тем же UPDATE, что и
счётчик, внутри той же транзакции, что INSERT в users. Иначе возможен
пользователь без отметки или отметка без пользователя — тесты ниже смотрят
на обе стороны сразу.

⚠️ ЧИТАЕМ СОСТОЯНИЕ БАЗЫ, А НЕ КОД ОТВЕТА (урок T166).
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
    await db.добавить_организацию(ORG, "АОЦГ")
    await db.добавить_пользователя(id=ADMIN_ID, first_name="Админ", role="admin")
    monkeypatch.setattr(auth_router, "email_enabled", lambda: False)
    return db


async def _ссылка(db, token, *, email, role="employee", is_active=True):
    await db.pool.execute(
        """INSERT INTO invite_links
             (token, org_id, role, created_by, expires_at, max_uses, email, is_active)
           VALUES ($1,$2,$3,$4,$5,1,$6,$7)""",
        token,
        ORG,
        role,
        ADMIN_ID,
        ЗАВТРА,
        email,
        is_active,
    )


async def _приглашение(db, token):
    строка = await db.pool.fetchrow("SELECT * FROM invite_links WHERE token=$1", token)
    return dict(строка) if строка else None


async def _войти(client, token, email):
    return await client.post(
        "/api/auth/register-by-invite",
        json={
            "token": token,
            "email": email,
            "password": "парольдлинный",
            "first_name": "Иван",
            "last_name": "Петров",
        },
    )


# ── 5: именная ссылка ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_именная_запоминает_кого_завели(db, орг, client):
    await _ссылка(db, "имен", email="ivan@example.com", role="accountant")
    r = await _войти(client, "имен", "ivan@example.com")
    assert r.status_code == 200, r.text

    новый = await db.pool.fetchrow(
        "SELECT id FROM users WHERE lower(email)='ivan@example.com'"
    )
    строка = await _приглашение(db, "имен")
    assert строка["used_by_user_id"] == новый["id"], (
        "отметка обязана указывать на ТОГО САМОГО пользователя"
    )
    assert строка["used_at"] is not None
    assert строка["uses_count"] == 1


# ── 6: общая ссылка ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_общая_запоминает_кого_завели(db, орг, client):
    await _ссылка(db, "obshaya", email=None)
    r = await _войти(client, "obshaya", "kto-ugodno@example.com")
    assert r.status_code == 200, r.text

    новый = await db.pool.fetchrow(
        "SELECT id FROM users WHERE lower(email)='kto-ugodno@example.com'"
    )
    строка = await _приглашение(db, "obshaya")
    assert строка["used_by_user_id"] == новый["id"]
    assert строка["used_at"] is not None


# ── 7: отказ не оставляет следа ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_отказ_не_ставит_отметку(db, орг, client):
    """Чужая почта → 400, и приглашение остаётся ровно таким, каким было."""
    await _ссылка(db, "имен", email="ivan@example.com")
    r = await _войти(client, "имен", "chuzhoy@example.com")
    assert r.status_code == 400

    строка = await _приглашение(db, "имен")
    assert строка["used_by_user_id"] is None
    assert строка["used_at"] is None
    assert строка["uses_count"] == 0
    assert строка["is_active"] is True


@pytest.mark.asyncio
async def test_пользователь_и_отметка_появляются_вместе(db, орг, client):
    """⚠️ ОБЕ СТОРОНЫ ОДНОГО ФАКТА. Не бывает пользователя без отметки и
    отметки без пользователя: и то и другое пишется одной транзакцией."""
    await _ссылка(db, "имен", email="ivan@example.com")
    людей_до = await db.pool.fetchval("SELECT count(*) FROM users")

    await _войти(client, "имен", "chuzhoy@example.com")  # отказ
    assert await db.pool.fetchval("SELECT count(*) FROM users") == людей_до
    assert (await _приглашение(db, "имен"))["used_by_user_id"] is None

    await _войти(client, "имен", "ivan@example.com")  # успех
    assert await db.pool.fetchval("SELECT count(*) FROM users") == людей_до + 1
    assert (await _приглашение(db, "имен"))["used_by_user_id"] is not None


# ── 8: старые строки не заполняются ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_старые_строки_остаются_пустыми(db, орг, client):
    """Погашенное приглашение из прошлого не получает значений ни от миграции,
    ни от чужой регистрации: связи не было, и выдумывать её нельзя."""
    await _ссылка(db, "старое", email="al@aocg.ru", is_active=False)
    await _ссылка(db, "новое", email="ivan@example.com")

    await _войти(client, "новое", "ivan@example.com")

    старое = await _приглашение(db, "старое")
    assert старое["used_by_user_id"] is None and старое["used_at"] is None


# ── Э5: список показывает отработавшие ──────────────────────────────────────
@pytest.mark.asyncio
async def test_список_показывает_отработавшие_и_кто_вошёл(db, орг, client):
    """⚠️ БЕЗ ЭТОГО ВСЯ РАБОТА УХОДИТ В ПУСТОТУ. Одноразовая ссылка гаснет
    сразу после регистрации: при фильтре `is_active = true` колонку «кого
    завели» не увидеть НИКОГДА, сколько её ни заполняй.
    """
    await _ссылка(db, "имен", email="ivan@example.com")
    await _войти(client, "имен", "ivan@example.com")

    список = (await client.get("/api/invite/list")).json()
    отработавшая = next((i for i in список if i["token"] == "имен"), None)
    assert отработавшая is not None, "погашенная ссылка пропала из списка"
    assert отработавшая["отработала"] is True
    assert отработавшая["вошёл"] == "Иван Петров", (
        "имя берётся из users по used_by_user_id, а не копией в приглашении"
    )
    assert отработавшая["used_at"] is not None


@pytest.mark.asyncio
async def test_живые_ссылки_идут_впереди_отработавших(db, орг, client):
    """Список не должен превратиться в свалку: с живыми ещё работают,
    отработавшие — история."""
    await _ссылка(db, "имен", email="ivan@example.com")
    await _войти(client, "имен", "ivan@example.com")
    await _ссылка(db, "живая", email="petr@example.com")

    список = (await client.get("/api/invite/list")).json()
    порядок = [i["token"] for i in список]
    assert порядок.index("живая") < порядок.index("имен")


@pytest.mark.asyncio
async def test_имя_вошедшего_следует_за_профилем(db, орг, client):
    """Имя не копируется в строку приглашения: поправили профиль — список
    показывает новое. Копия соврала бы молча."""
    await _ссылка(db, "имен", email="ivan@example.com")
    await _войти(client, "имен", "ivan@example.com")
    await db.pool.execute(
        "UPDATE users SET last_name='Сидоров' WHERE lower(email)='ivan@example.com'"
    )

    список = (await client.get("/api/invite/list")).json()
    строка = next(i for i in список if i["token"] == "имен")
    assert строка["вошёл"] == "Иван Сидоров"


@pytest.mark.asyncio
async def test_у_невостребованной_ссылки_поля_пустые(db, орг, client):
    await _ссылка(db, "ждёт", email="petr@example.com")
    список = (await client.get("/api/invite/list")).json()
    строка = next(i for i in список if i["token"] == "ждёт")
    assert строка["отработала"] is False
    assert строка["вошёл"] is None and строка["used_at"] is None
