# -*- coding: utf-8 -*-
"""Восстановление пароля (S-56).

⚠️ ГЛАВНОЕ ЗДЕСЬ НЕ «РАБОТАЕТ ЛИ СБРОС», А ЧТО РУЧКА НЕ ВЫДАЁТ СПИСОК НАШИХ
КЛИЕНТОВ. Восстановление — самое удобное место узнать, зарегистрирован ли
адрес: «письмо отправлено» против «пользователь не найден» превращает форму
входа в справочник. Поэтому проверок на неразличимость больше, чем на сам
сброс, и они стоят первыми.

ССЫЛКА ИЗ ПИСЬМА — КЛЮЧ НА ПРЕДЪЯВИТЕЛЕ, живущий в почтовом ящике. Отсюда
остальные проверки: одноразовость, срок, гашение прежних ссылок и всех
выданных токенов, проверка is_active В МОМЕНТ ПРИМЕНЕНИЯ.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

import app.routers.auth as auth_router
from app.auth import get_current_user, hash_password, verify_password
from app.main import app

ПАРОЛЬ = "старый-пароль-1"
ХЕШ = hash_password(ПАРОЛЬ)
НОВЫЙ = "новый-пароль-длинный"
ЕСТЬ = "ivan@example.com"
НЕТ = "nikogo@example.com"


@pytest.fixture(autouse=True)
def без_ограничителя():
    """Ограничитель по сетевому адресу отключён: он про другое (S-31).

    Лимит ПО АДРЕСУ ПОЧТЫ при этом остаётся живым — он в самой ручке,
    и именно он проверяется ниже.
    """
    auth_router.limiter.enabled = False
    yield
    auth_router.limiter.enabled = True


@pytest.fixture
def почта_включена(monkeypatch):
    """Письма «уходят»: список вызовов вместо отправки."""
    следы = []
    monkeypatch.setattr(auth_router, "email_enabled", lambda: True)
    monkeypatch.setattr(
        auth_router,
        "send_password_reset_email",
        lambda *а, **к: следы.append(а) or True,
    )
    return следы


@pytest.fixture
def пользователь(db):
    db.organizations.append(dict(id=1, name="ООО Ромашка", inn="7700000000"))
    row = dict(
        id=7,
        first_name="Иван",
        last_name="Петров",
        email=ЕСТЬ,
        password_hash=ХЕШ,
        role="employee",
        org_id=1,
        is_active=True,
        is_email_verified=True,
        failed_attempts=0,
        locked_until=None,
        tokens_valid_from=None,
    )
    db.users.append(row)
    return row


def _токен_из(следы):
    """Токен вынимается из ссылки, как его получил бы человек из письма."""
    return следы[-1][1].split("token=")[1]


# ─── ГЛАВНОЕ: ручка не различает существующий и несуществующий адрес ───


async def test_ответ_одинаков_для_существующего_и_несуществующего(
    client, пользователь, почта_включена
):
    """⚠️ Ловит мутанта «вернуть 404 для неизвестного адреса»."""
    есть = await client.post("/api/auth/forgot-password", json={"email": ЕСТЬ})
    нет = await client.post("/api/auth/forgot-password", json={"email": НЕТ})
    assert есть.status_code == нет.status_code == 200
    assert есть.json() == нет.json(), "тела ответов различаются — адрес выдан"


async def test_письмо_уходит_только_существующему(client, пользователь, почта_включена):
    await client.post("/api/auth/forgot-password", json={"email": НЕТ})
    assert почта_включена == [], "письмо ушло на незарегистрированный адрес"
    await client.post("/api/auth/forgot-password", json={"email": ЕСТЬ})
    assert len(почта_включена) == 1
    assert почта_включена[0][0] == ЕСТЬ
    assert "/reset-password?token=" in почта_включена[0][1]


async def test_попытка_считается_и_для_несуществующего_адреса(
    client, db, пользователь, почта_включена
):
    """⚠️ РАВЕНСТВО ПОРОГОВ, и это не педантизм.

    Считай мы попытки только для зарегистрированных адресов, лимиты стали бы
    разными — 3 в час на наш адрес и 20 на чужой. Ответ при этом одинаков,
    а ПОВЕДЕНИЕ различается: перебирающий узнаёт наши адреса по тому, где
    лимит наступает раньше. Ловит мутанта «считать только существующие».
    """
    await client.post("/api/auth/forgot-password", json={"email": НЕТ})
    assert len(db.reset_attempts) == 1, "попытка по несуществующему адресу не учтена"


async def test_лимит_по_адресу_не_меняет_ответ(
    client, db, пользователь, почта_включена
):
    """Четвёртый запрос за час: письма нет, ответ прежний."""
    ответы = []
    for _ in range(4):
        r = await client.post("/api/auth/forgot-password", json={"email": ЕСТЬ})
        ответы.append((r.status_code, r.json()))
    assert len(set(map(str, ответы))) == 1, "ответ изменился после лимита"
    assert len(почта_включена) == 3, "лимит по адресу не сработал"


# ─── сам сброс ───


async def test_сброс_меняет_пароль_и_гасит_все_токены(
    client, db, пользователь, почта_включена
):
    await client.post("/api/auth/forgot-password", json={"email": ЕСТЬ})
    r = await client.post(
        "/api/auth/reset-password",
        json={"token": _токен_из(почта_включена), "new_password": НОВЫЙ},
    )
    assert r.status_code == 200
    assert verify_password(НОВЫЙ, пользователь["password_hash"])
    assert not verify_password(ПАРОЛЬ, пользователь["password_hash"])
    assert пользователь["tokens_valid_from"] is not None, (
        "прежние токены не погашены — ради этого пароль и восстанавливают"
    )


async def test_ссылка_срабатывает_один_раз(client, db, пользователь, почта_включена):
    await client.post("/api/auth/forgot-password", json={"email": ЕСТЬ})
    токен = _токен_из(почта_включена)
    первый = await client.post(
        "/api/auth/reset-password", json={"token": токен, "new_password": НОВЫЙ}
    )
    второй = await client.post(
        "/api/auth/reset-password",
        json={"token": токен, "new_password": "ещё-другой-1"},
    )
    assert первый.status_code == 200
    assert второй.status_code == 400


async def test_истёкшая_ссылка_не_работает(client, db, пользователь, почта_включена):
    await client.post("/api/auth/forgot-password", json={"email": ЕСТЬ})
    токен = _токен_из(почта_включена)
    db.password_resets[-1]["expires_at"] = datetime.now(timezone.utc) - timedelta(
        minutes=1
    )
    r = await client.post(
        "/api/auth/reset-password", json={"token": токен, "new_password": НОВЫЙ}
    )
    assert r.status_code == 400
    assert verify_password(ПАРОЛЬ, пользователь["password_hash"]), (
        "пароль всё же сменён"
    )


async def test_ссылка_уволенного_не_работает(client, db, пользователь, почта_включена):
    """⚠️ Проверка is_active В МОМЕНТ ПРИМЕНЕНИЯ, а не только при выдаче.

    Удаление пользователя в проекте мягкое: строка жива, почта та же. Значит
    ссылка, запрошенная до увольнения, осталась бы ключом от учётной записи.
    """
    await client.post("/api/auth/forgot-password", json={"email": ЕСТЬ})
    токен = _токен_из(почта_включена)
    пользователь["is_active"] = False
    r = await client.post(
        "/api/auth/reset-password", json={"token": токен, "new_password": НОВЫЙ}
    )
    assert r.status_code == 400
    assert verify_password(ПАРОЛЬ, пользователь["password_hash"])


async def test_новый_запрос_гасит_прежнюю_ссылку(
    client, db, пользователь, почта_включена
):
    """Иначе десять запросов дают десять живых ключей от одной записи."""
    await client.post("/api/auth/forgot-password", json={"email": ЕСТЬ})
    первый = _токен_из(почта_включена)
    await client.post("/api/auth/forgot-password", json={"email": ЕСТЬ})
    r = await client.post(
        "/api/auth/reset-password", json={"token": первый, "new_password": НОВЫЙ}
    )
    assert r.status_code == 400


async def test_смена_пароля_вручную_гасит_невостребованную_ссылку(
    client, db, пользователь, почта_включена
):
    """Человек не дождался письма и сменил пароль сам — ссылка обязана умереть."""
    await client.post("/api/auth/forgot-password", json={"email": ЕСТЬ})
    токен = _токен_из(почта_включена)
    app.dependency_overrides[get_current_user] = lambda: dict(пользователь)
    смена = await client.post(
        "/api/users/me/change-password",
        json={"old_password": ПАРОЛЬ, "new_password": НОВЫЙ},
    )
    app.dependency_overrides.clear()
    assert смена.status_code == 200
    r = await client.post(
        "/api/auth/reset-password",
        json={"token": токен, "new_password": "третий-пароль"},
    )
    assert r.status_code == 400, "ссылка пережила смену пароля"


async def test_отказ_одинаков_во_всех_случаях_промаха(
    client, db, пользователь, почта_включена
):
    """⚠️ Разные тексты различали бы состояния ЧУЖИХ ссылок.

    «Истекла» против «уже использована» подсказывает перебирающему, что токен
    он угадал верно, а не наоборот.
    """
    await client.post("/api/auth/forgot-password", json={"email": ЕСТЬ})
    токен = _токен_из(почта_включена)
    выдуманный = await client.post(
        "/api/auth/reset-password", json={"token": "нет-такого", "new_password": НОВЫЙ}
    )
    await client.post(
        "/api/auth/reset-password", json={"token": токен, "new_password": НОВЫЙ}
    )
    использованный = await client.post(
        "/api/auth/reset-password", json={"token": токен, "new_password": НОВЫЙ}
    )
    assert выдуманный.status_code == использованный.status_code == 400
    assert выдуманный.json() == использованный.json()


async def test_короткий_пароль_не_тратит_ссылку(
    client, db, пользователь, почта_включена
):
    """Опечатка в пароле не должна стоить человеку второго письма."""
    await client.post("/api/auth/forgot-password", json={"email": ЕСТЬ})
    токен = _токен_из(почта_включена)
    короткий = await client.post(
        "/api/auth/reset-password", json={"token": токен, "new_password": "мало"}
    )
    assert короткий.status_code == 400
    снова = await client.post(
        "/api/auth/reset-password", json={"token": токен, "new_password": НОВЫЙ}
    )
    assert снова.status_code == 200, "ссылка сгорела на неверном пароле"


async def test_токен_в_базе_хранится_хешем(client, db, пользователь, почта_включена):
    """Утечка базы не должна давать работающих ссылок."""
    await client.post("/api/auth/forgot-password", json={"email": ЕСТЬ})
    токен = _токен_из(почта_включена)
    хранится = db.password_resets[-1]["token_hash"]
    assert хранится != токен
    assert хранится == hashlib.sha256(токен.encode()).hexdigest()
