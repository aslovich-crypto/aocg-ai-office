"""Отзыв токенов отметкой `tokens_valid_from` (S-16).

ЧТО ПРОВЕРЯЕТСЯ. Отзыв у нас сделан НЕ чёрным списком: в строке
пользователя стоит отметка, и токен, выданный раньше неё, недействителен.
Проверка живёт в `get_current_user` и не стоит лишнего обращения к базе —
строка и так читается на каждом вызове API.

ОБЕ ПОЛОВИНЫ У КАЖДОЙ ПРОВЕРКИ (правило CLAUDE.md): мало убедиться, что
отозванный токен получил 401 — рядом обязан стоять ЖИВОЙ случай, иначе
защита, выгнавшая вообще всех, читалась бы как успех. Поэтому у каждого
случая отзыва есть сосед: другой пользователь продолжает работать.

ЗДЕСЬ НЕ ПРОВЕРЯЕТСЯ (называю границу): что отметка попала именно в базу
прода — это FakePool, он зеркалит SQL вручную. Настоящий PostgreSQL
проверяет отдельный шаг CI (`sql_check`), а поведение хендлеров — T36 п.2.
"""

from datetime import datetime, timedelta, timezone

import pytest

import app.routers.auth as auth_router
from jose import jwt

from app.auth import (
    JWT_ALGORITHM,
    JWT_SECRET,
    create_access_token,
    get_current_user,
    hash_password,
)
from app.main import app

ПАРОЛЬ = "правильный-пароль"
ХЕШ = hash_password(ПАРОЛЬ)


@pytest.fixture(autouse=True)
def без_ограничителя():
    auth_router.limiter.enabled = False
    yield
    auth_router.limiter.enabled = True


@pytest.fixture
def клиент(client):
    """Клиент с НАСТОЯЩЕЙ проверкой токена.

    Общая фикстура `client` подменяет `get_current_user` фиксированным
    администратором — иначе тестам пришлось бы носить JWT в каждом запросе.
    Здесь подмена СНИМАЕТСЯ: проверяется как раз то, что делает эта
    зависимость, и с подменой все проверки ниже измеряли бы пустоту.
    Ошибка была допущена в первой версии этого файла и поймана падением
    на состоянии (отметка не появилась), а не на коде ответа.
    """
    app.dependency_overrides.pop(get_current_user, None)
    return client


@pytest.fixture
def двое(db):
    """Двое в одной организации: тот, кого отзывают, и сосед."""
    db.organizations.append(dict(id=1, name="ООО Ромашка", inn="7700000000"))
    первый = dict(
        id=7,
        first_name="Иван",
        last_name="Петров",
        email="ivan@example.com",
        password_hash=ХЕШ,
        role="employee",
        org_id=1,
        is_active=True,
        is_email_verified=True,
        failed_attempts=0,
        locked_until=None,
        tokens_valid_from=None,
    )
    второй = dict(
        id=8,
        first_name="Пётр",
        last_name="Иванов",
        email="petr@example.com",
        password_hash=ХЕШ,
        role="employee",
        org_id=1,
        is_active=True,
        is_email_verified=True,
        failed_attempts=0,
        locked_until=None,
        tokens_valid_from=None,
    )
    db.users.extend([первый, второй])
    db._uid = 8
    return первый, второй


def заголовок(user_id: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def заголовок_старый(user_id: int, минут_назад: int = 5) -> dict:
    """Токен, выданный ЗАМЕТНО РАНЬШЕ отзыва.

    Почему не `create_access_token`: `iat` в JWT — целые секунды, и отметка
    отзыва округляется до секунды тоже (иначе свежая пара умирала бы сразу
    после выдачи). Значит токен, выданный в ТУ ЖЕ секунду, что и отзыв,
    переживает его — окно в одну секунду, названное в коде сознательно.
    Тест про «чужой токен перестал работать» обязан моделировать реальность:
    у злоумышленника токен получен РАНЬШЕ, а не в тот же миг. Иначе тест
    проверял бы совпадение часов, а не отзыв.
    """
    выдан = datetime.now(timezone.utc) - timedelta(minutes=минут_назад)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": выдан,
        "exp": выдан + timedelta(hours=1),
    }
    токен = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {"Authorization": f"Bearer {токен}"}


# ───────────────────────── смена пароля гасит токены ─────────────────────────


@pytest.mark.asyncio
async def test_смена_пароля_гасит_прежний_токен(клиент, двое):
    """Случай, ради которого пароль и меняют: «зашли из чужого места»."""
    первый, второй = двое
    старый = заголовок_старый(первый["id"])

    # До смены токен рабочий — иначе дальнейший 401 ничего не доказывает.
    assert (await клиент.get("/api/users/me", headers=старый)).status_code == 200

    r = await клиент.post(
        "/api/users/me/change-password",
        json={"old_password": ПАРОЛЬ, "new_password": "новый-длинный-пароль"},
        headers=старый,
    )
    assert r.status_code == 200, r.text

    # СОСТОЯНИЕ, а не только код ответа: отметка проставлена.
    assert первый["tokens_valid_from"] is not None, (
        "смена пароля обязана ставить отметку отзыва, иначе старый токен живёт"
    )

    # Прежний токен мёртв.
    assert (await клиент.get("/api/users/me", headers=старый)).status_code == 401

    # А выданная в ответе пара — рабочая: человек не выброшен из системы.
    новый = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await клиент.get("/api/users/me", headers=новый)).status_code == 200


@pytest.mark.asyncio
async def test_чужая_смена_пароля_не_трогает_соседа(клиент, двое):
    """Положительная половина: гейт, выгнавший всех, — не успех."""
    первый, второй = двое
    сосед = заголовок(второй["id"])
    await клиент.post(
        "/api/users/me/change-password",
        json={"old_password": ПАРОЛЬ, "new_password": "новый-длинный-пароль"},
        headers=заголовок(первый["id"]),
    )
    assert (await клиент.get("/api/users/me", headers=сосед)).status_code == 200
    assert второй["tokens_valid_from"] is None


# ───────────────────────── выйти на всех устройствах ─────────────────────────


@pytest.mark.asyncio
async def test_выйти_везде_гасит_все_токены(клиент, двое):
    первый, второй = двое
    токен = заголовок_старый(первый["id"])
    assert (await клиент.get("/api/users/me", headers=токен)).status_code == 200

    r = await клиент.post("/api/auth/logout-all", headers=токен)
    assert r.status_code == 200, r.text
    assert первый["tokens_valid_from"] is not None

    assert (await клиент.get("/api/users/me", headers=токен)).status_code == 401
    # Сосед по-прежнему работает.
    assert (
        await клиент.get("/api/users/me", headers=заголовок(второй["id"]))
    ).status_code == 200


@pytest.mark.asyncio
async def test_токен_выданный_после_отзыва_работает(клиент, двое):
    """Отзыв — граница во времени, а не запрет навсегда."""
    первый, _ = двое
    await клиент.post("/api/auth/logout-all", headers=заголовок(первый["id"]))
    свежий = заголовок(первый["id"])  # выдан ПОСЛЕ отметки
    assert (await клиент.get("/api/users/me", headers=свежий)).status_code == 200


# ───────────────────────── чистка отозванных refresh ─────────────────────────


@pytest.mark.asyncio
async def test_выход_чистит_просроченные_отзывы(клиент, двое, db):
    """Чистка привязана к действию, которое таблицу и растит."""
    протухший = dict(
        token_hash="старый",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    живой = dict(
        token_hash="свежий",
        expires_at=datetime.now(timezone.utc) + timedelta(days=10),
    )
    db.revoked_tokens.extend([протухший, живой])

    r = await клиент.post("/api/auth/logout", json={"refresh_token": "какой-то"})
    assert r.status_code == 200, r.text

    хеши = {t["token_hash"] for t in db.revoked_tokens}
    assert "старый" not in хеши, "просроченная запись обязана удаляться"
    assert "свежий" in хеши, "живой отзыв удалять нельзя — это отключило бы защиту"
