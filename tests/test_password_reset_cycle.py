# -*- coding: utf-8 -*-
"""Полный круг восстановления: forgot → reset → ВХОД (S-56).

⚠️ ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ, ЕСЛИ ЕСТЬ test_password_reset.py С 13 ТЕСТАМИ.
Те тринадцать проверяли, что хеш в базе сменился, — и ни один не спросил,
ПУСКАЮТ ли новым паролем. Проверка обрывалась ровно там, где начиналась беда:
26.08.2026 на проде сброс отработал (200, хеш сменился), а вход отдал 403,
потому что `is_email_verified` остался false. Тест «до входа» этого не видит
в принципе — сколько его ни повторяй.

ЗДЕСЬ ПРОВЕРЯЕТСЯ ИМЕННО ВХОД, то есть последняя дверь, ради которой всё
и делалось.
"""

from datetime import datetime, timedelta, timezone

import pytest

import app.routers.auth as auth_router
from app.auth import hash_password

СТАРЫЙ = "старый-пароль-1"
НОВЫЙ = "новый-пароль-длинный"
ПОЧТА = "krug@example.com"


@pytest.fixture(autouse=True)
def без_ограничителя():
    auth_router.limiter.enabled = False
    yield
    auth_router.limiter.enabled = True


@pytest.fixture
def почта_включена(monkeypatch):
    следы = []
    monkeypatch.setattr(auth_router, "email_enabled", lambda: True)
    monkeypatch.setattr(
        auth_router,
        "send_password_reset_email",
        lambda *а, **к: следы.append(а) or True,
    )
    return следы


@pytest.fixture
def неподтверждённый(db):
    """Тот самый случай с прода: адрес не подтверждён, замок взведён.

    Регистрация при ВКЛЮЧЁННОЙ почте ставит is_email_verified=false
    (auth.py: auto_verify = not email_enabled()), и по ссылке человек
    не переходил. Плюс пять неудачных попыток входа — то, что и приводит
    человека в восстановление.
    """
    db.organizations.append(dict(id=1, name="ООО Ромашка", inn="7700000000"))
    строка = dict(
        id=11,
        first_name="Пётр",
        last_name="Сидоров",
        email=ПОЧТА,
        phone=None,
        password_hash=hash_password(СТАРЫЙ),
        role="employee",
        org_id=1,
        is_active=True,
        is_email_verified=False,
        failed_attempts=5,
        locked_until=datetime.now(timezone.utc) + timedelta(minutes=15),
        tokens_valid_from=None,
    )
    db.users.append(строка)
    return строка


async def _сбросить(client, следы):
    await client.post("/api/auth/forgot-password", json={"email": ПОЧТА})
    токен = следы[-1][1].split("token=")[1]
    return await client.post(
        "/api/auth/reset-password", json={"token": токен, "new_password": НОВЫЙ}
    )


async def test_после_сброса_пускают_новым_паролем(
    client, db, неподтверждённый, почта_включена
):
    """ГЛАВНЫЙ. Именно этого теста не было, и именно это сломалось на проде."""
    сброс = await _сбросить(client, почта_включена)
    assert сброс.status_code == 200

    вход = await client.post(
        "/api/auth/login", json={"phone_or_email": ПОЧТА, "password": НОВЫЙ}
    )
    assert вход.status_code == 200, (
        f"после успешного сброса вход отбит {вход.status_code}: {вход.json()}"
    )
    assert вход.json().get("access_token")


async def test_сброс_снимает_замок_счётчика(
    client, db, неподтверждённый, почта_включена
):
    """Замок взводится ДО восстановления — иначе выхода из петли нет.

    Ровно об этом написано в разборе S-59: «если пароль не вспомнить,
    а восстановления нет, выхода из петли не существовало вовсе».
    Восстановление появилось — петля обязана закрыться целиком.
    """
    await _сбросить(client, почта_включена)
    assert неподтверждённый["failed_attempts"] == 0
    assert неподтверждённый["locked_until"] is None


async def test_ворота_почты_называют_себя_кодом(client, db, почта_включена):
    """403 обязан нести машиночитаемую причину, а не только текст.

    Фронт сейчас показывает 403 тем же сообщением, что и 401 («неверный
    пароль»), и человек идёт менять пароль, который и так верен. Текст
    для этого не годится: по нему нельзя ветвиться, не сверяя строки.
    """
    db.organizations.append(dict(id=1, name="ООО Ромашка", inn="7700000000"))
    db.users.append(
        dict(
            id=12,
            email="nepodtv@example.com",
            phone=None,
            password_hash=hash_password(СТАРЫЙ),
            role="employee",
            org_id=1,
            is_active=True,
            is_email_verified=False,
            failed_attempts=0,
            locked_until=None,
        )
    )
    r = await client.post(
        "/api/auth/login",
        json={"phone_or_email": "nepodtv@example.com", "password": СТАРЫЙ},
    )
    assert r.status_code == 403
    тело = r.json()
    assert тело.get("code") == "email_not_verified", тело
    assert тело.get("detail") == "Подтвердите email", тело
