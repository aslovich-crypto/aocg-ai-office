# -*- coding: utf-8 -*-
"""Выдача пользователей — белый список полей (S-72, на живой базе).

⚠️ СТРОКА S-72 ОПИСЫВАЛА НЕ ТО МЕСТО, И ЭТО НАШЛОСЬ ОПИСЬЮ 04.09.2026.
Она утверждала, что `failed_attempts` и `locked_until` отдаёт `/api/users/me`.
На самом деле `/me` собирается белым списком с 21.05.2026 — счётчиков там
нет. А вот СПИСОК `/api/users/` отдавал управляющим ролям строку через
ЧЁРНЫЙ список (прятал только пароль и токен подтверждения), и счётчики
уходили именно оттуда.

⚠️ ПОЧЕМУ ЭТО КЛАСС, А НЕ ОДНА УТЕЧКА — довод, принятый владельцем: при
чёрном списке ЛЮБАЯ новая колонка в `users` уезжает наружу сама собой,
и заметить это можно только вручную. Белый список делает утечку новой
колонки невозможной по устройству.

⚠️ ПОЧЕМУ НА ЖИВОЙ БАЗЕ. Проверяется, что наружу не уходят РЕАЛЬНЫЕ колонки
таблицы. На двойнике в строке лежит ровно то, что положил тест, — он не
знает про `email_verify_expires_at` и не узнает про колонку, добавленную
завтра. Живая база отдаёт `SELECT *` со всеми колонками, какие есть.
"""

import pytest

# Колонки, которых в ответе быть не должно ни при какой роли.
ЗАПРЕЩЕНО = (
    "password_hash",
    "email_verify_token",
    "failed_attempts",
    "locked_until",
    "email_verify_expires_at",
    "tokens_valid_from",
)


@pytest.mark.asyncio
async def test_управляющий_не_видит_внутреннюю_механику(client, db, seeded):
    """Админ получает кадровую карточку, а не внутренности защиты."""
    await db.pool.execute(
        "UPDATE users SET failed_attempts=3, locked_until=NOW() + INTERVAL '15 min', "
        "password_hash='секрет', email_verify_token='токен' WHERE id=2"
    )
    r = await client.get("/api/users/")
    assert r.status_code == 200, r.text
    (строка,) = [u for u in r.json() if u["id"] == 2]
    for поле in ЗАПРЕЩЕНО:
        assert поле not in строка, f"{поле} уехало наружу"
    # И положительная половина: то, ради чего список существует.
    for нужное in ("first_name", "last_name", "email", "role", "is_active"):
        assert нужное in строка, f"{нужное} пропало из кадровой карточки"


@pytest.mark.asyncio
async def test_новая_колонка_наружу_сама_не_уедет(client, db, seeded):
    """⚠️ ГЛАВНОЕ УТВЕРЖДЕНИЕ: белый список против чёрного.

    Заводим колонку, какой в коде ещё нет, — ровно то, что случится, когда
    в `users` добавят следующее поле. При чёрном списке она уехала бы
    наружу молча; при белом её там нет, и никто ничего не забыл.
    """
    await db.pool.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS секретное_поле TEXT"
    )
    await db.pool.execute("UPDATE users SET секретное_поле='нельзя наружу' WHERE id=2")
    r = await client.get("/api/users/")
    (строка,) = [u for u in r.json() if u["id"] == 2]
    assert "секретное_поле" not in строка
    assert "нельзя наружу" not in str(строка)


@pytest.mark.asyncio
async def test_сотрудник_по_прежнему_видит_только_имена(client_employee, db, seeded):
    """S-28 не отменяется: рядовому сотруднику — id и ФИО, и всё."""
    r = await client_employee.get("/api/users/")
    (строка,) = [u for u in r.json() if u["id"] == 2]
    assert set(строка) == {"id", "first_name", "last_name", "patronymic"}


@pytest.mark.asyncio
async def test_свой_профиль_тоже_без_счётчиков(client, db, seeded):
    """`/me` собирался белым списком и раньше — проверяем, что так и есть."""
    await db.добавить_пользователя(id=1, first_name="Админ", role="admin")
    await db.pool.execute("UPDATE users SET failed_attempts=5 WHERE id=1")
    r = await client.get("/api/users/me")
    assert r.status_code == 200, r.text
    for поле in ЗАПРЕЩЕНО:
        assert поле not in r.json(), f"{поле} уехало наружу в /me"
