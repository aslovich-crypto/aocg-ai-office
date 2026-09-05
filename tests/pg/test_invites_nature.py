# -*- coding: utf-8 -*-
"""Природа приглашения выбирается ОДИН РАЗ, при создании — НА ЖИВОЙ БАЗЕ.

⚠️ ЗАЧЕМ, ЗАМЕРОМ ПРОДА 04.09.2026 (бастион). В `invite_links` 8 строк, у пяти
`expires_at` ПУСТ — бессрочные, включая последнюю выпущенную. Причина не в
невнимательности: во фронте срок стартовал пустым, а «Бессрочная» стояла
обычным вариантом в ряду. Плюс природу ссылки определяла НАЖАТАЯ КНОПКА —
«Скопировать ссылку» слала `email: null`, и введённый адрес молча отбрасывался.

⚠️ ПРОВЕРЯЕМ СОСТОЯНИЕ В БАЗЕ, А НЕ КОД ОТВЕТА. 200 можно вернуть и не записав
почту — ровно этот разрыв ловили в T166: там ответ был честный, а состав отчёта
уезжал молча. Поэтому каждый тест читает строку `invite_links` и смотрит, что
там на самом деле лежит.

Правила (решение владельца 05.09.2026):
  почта заполнена → ИМЕННАЯ: роль и срок из формы, срок ОБЯЗАТЕЛЕН;
  почта пуста     → ОБЩАЯ:   роль employee и срок сутки ПРИНУДИТЕЛЬНО.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.routers import auth as auth_router

ADMIN_ID = 1
ORG = 1


@pytest_asyncio.fixture
async def админ(db, monkeypatch):
    """Организация, админ и заглушённая почта: письмо здесь не предмет проверки."""
    await db.добавить_организацию(ORG, "АОЦГ")
    await db.добавить_пользователя(id=ADMIN_ID, first_name="Админ", role="admin")
    monkeypatch.setattr(auth_router, "send_invite_notification", lambda *a: True)
    return db


async def _приглашение(db, token):
    строка = await db.pool.fetchrow("SELECT * FROM invite_links WHERE token=$1", token)
    return dict(строка) if строка else None


# ── 4: срок обязателен ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_именная_без_срока_отказ_и_строки_нет(db, админ, client):
    r = await client.post(
        "/api/invite/create",
        json={"role": "accountant", "email": "kto@example.com"},
    )
    assert r.status_code == 400
    assert "срок" in r.json()["detail"].lower()
    # ⚠️ Отказ обязан быть ДО записи: «сказали нет» и «ничего не создали» —
    # разные утверждения, и второе здесь важнее.
    assert await db.pool.fetchval("SELECT count(*) FROM invite_links") == 0


@pytest.mark.asyncio
async def test_нулевой_и_отрицательный_срок_тоже_отказ(db, админ, client):
    for часы in (0, -24):
        r = await client.post(
            "/api/invite/create",
            json={
                "role": "employee",
                "email": "kto@example.com",
                "expires_hours": часы,
            },
        )
        assert r.status_code == 400, часы
    assert await db.pool.fetchval("SELECT count(*) FROM invite_links") == 0


# ── 5 и 7: общая ссылка — роль и срок принудительные ────────────────────────
@pytest.mark.asyncio
async def test_без_почты_ссылка_общая_роль_employee_срок_сутки(db, админ, client):
    до = datetime.now(timezone.utc)
    r = await client.post("/api/invite/create", json={"role": "employee"})
    assert r.status_code == 200, r.text
    строка = await _приглашение(db, r.json()["token"])
    assert строка["email"] is None
    assert строка["role"] == "employee"
    assert строка["expires_at"] is not None, "бессрочных ссылок больше не бывает"
    # Сутки ± минута прогона: сравниваем окном, а не равенством — иначе тест
    # ловил бы скорость машины, а не правило.
    assert (
        timedelta(hours=23, minutes=59)
        <= строка["expires_at"] - до
        <= timedelta(hours=24, minutes=1)
    )


@pytest.mark.asyncio
async def test_роль_из_тела_при_пустой_почте_НЕ_проходит(db, админ, client):
    """Прислали admin — в базе обязан лежать employee.

    ⚠️ Смотрим в БАЗУ, а не в ответ: ответ можно починить, оставив запись
    прежней, и наоборот. Права даёт то, что лежит в строке.
    """
    r = await client.post(
        "/api/invite/create", json={"role": "admin", "expires_hours": 720}
    )
    assert r.status_code == 200, r.text
    строка = await _приглашение(db, r.json()["token"])
    assert строка["role"] == "employee"
    assert r.json()["role"] == "employee", "ответ обязан говорить о записанном"


@pytest.mark.asyncio
async def test_срок_общей_ссылки_не_растягивается_телом(db, админ, client):
    """Прислали 720 часов — у общей всё равно сутки."""
    до = datetime.now(timezone.utc)
    r = await client.post(
        "/api/invite/create", json={"role": "employee", "expires_hours": 720}
    )
    строка = await _приглашение(db, r.json()["token"])
    assert строка["expires_at"] - до <= timedelta(hours=24, minutes=1)


# ── 6: почта не теряется НИ ПРИ КАКОЙ доставке ──────────────────────────────
@pytest.mark.asyncio
async def test_почта_сохранена_в_базе_при_обеих_кнопках(db, админ, client):
    """⚠️ СТОРОЖ НА МОЛЧАЛИВОЕ ОТБРАСЫВАНИЕ ПОЧТЫ — то, что случилось на проде.

    Кнопки «Отправить приглашение» и «Скопировать ссылку» обязаны давать ОДНУ
    И ТУ ЖЕ именную ссылку: они различаются доставкой, а не природой. С точки
    зрения бэкенда обе шлют один и тот же запрос — и обе обязаны сохранить
    адрес. Раньше вторая кнопка слала `email: null`, и адрес пропадал молча.
    """
    for подпись in ("отправить", "скопировать"):
        r = await client.post(
            "/api/invite/create",
            json={
                "role": "accountant",
                "email": f"{подпись}@Example.COM",
                "first_name": "Пётр",
                "expires_hours": 168,
            },
        )
        assert r.status_code == 200, r.text
        строка = await _приглашение(db, r.json()["token"])
        # Адрес приводится к нижнему регистру: один человек не должен
        # заводиться дважды разным написанием.
        assert строка["email"] == f"{подпись}@example.com"
        assert строка["role"] == "accountant", "именная ссылка держит роль формы"
        assert строка["expires_at"] is not None


@pytest.mark.asyncio
async def test_именная_ссылка_держит_срок_формы(db, админ, client):
    до = datetime.now(timezone.utc)
    r = await client.post(
        "/api/invite/create",
        json={"role": "employee", "email": "kto@example.com", "expires_hours": 168},
    )
    строка = await _приглашение(db, r.json()["token"])
    прошло = строка["expires_at"] - до
    assert timedelta(hours=167, minutes=59) <= прошло <= timedelta(hours=168, minutes=1)


@pytest.mark.asyncio
async def test_пробелы_вместо_почты_читаются_как_общая_ссылка(db, админ, client):
    """«   » — это не адрес. Иначе ссылка притворялась бы именной, а сверять
    при регистрации было бы нечего."""
    r = await client.post(
        "/api/invite/create",
        json={"role": "admin", "email": "   ", "expires_hours": 720},
    )
    assert r.status_code == 200, r.text
    строка = await _приглашение(db, r.json()["token"])
    assert строка["email"] is None
    assert строка["role"] == "employee"
