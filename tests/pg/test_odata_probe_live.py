# -*- coding: utf-8 -*-
"""Ручка-проба связи с 1С: кому видно и что отвечает (1C-21, заход ①).

⚠️ ЖИВОЙ КОНТУР НУЖЕН ЗДЕСЬ РАДИ ОДНОГО — ГЕЙТА. Проверяется не расчёт,
а то, что зависимость `get_current_user` и роль действительно режут доступ
в собранном приложении. На двойнике это была бы проверка зеркала.

⚠️⚠️ НАСТОЯЩАЯ 1С НЕ УЧАСТВУЕТ. Клиент подменяется функцией, как
подменяется `s3.get_object` в `test_integration_photo_live.py`. **Зелёный
прогон здесь НЕ означает, что прод доходит до 1С:** это выясняется только
живым запросом к ручке НА ПРОДЕ, и ничем иным.
"""

import pytest

from app import odata_client

ПРОБА = "/api/odata/probe"


async def _орг(db):
    await db.добавить_организацию(id=1, name="АОЦГ")
    await db.обеспечить_пользователя(id=1, first_name="Админ", role="admin")
    await db.обеспечить_пользователя(id=2, first_name="Иван", role="employee")


# ── ГЕЙТ ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_проба_только_администратору(client, as_role, db, monkeypatch):
    """Бухгалтеру тоже нельзя: это сведения об устройстве интеграции."""
    await _орг(db)
    monkeypatch.setattr(odata_client, "настроено", lambda: False)
    for роль in ("accountant", "employee"):
        as_role(роль)
        ответ = await client.get(ПРОБА)
        assert ответ.status_code == 403, "%s получил пробу: %s" % (роль, ответ.text)


@pytest.mark.asyncio
async def test_администратору_проба_доступна(client, db, monkeypatch):
    await _орг(db)
    monkeypatch.setattr(odata_client, "настроено", lambda: False)
    assert (await client.get(ПРОБА)).status_code == 200


# ── ЧТО ОТВЕЧАЕТ ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_не_настроено_отличимо_от_не_дошли(client, db, monkeypatch):
    """⚠️ РАЗНИЦА СУЩЕСТВЕННАЯ: первое чинится переменными окружения,
    второе — сетью площадки. Один общий текст заставил бы владельца
    искать не там."""
    await _орг(db)
    monkeypatch.setattr(odata_client, "настроено", lambda: False)
    тело = (await client.get(ПРОБА)).json()
    assert тело["дошли"] is False and тело["status"] == "odata_not_configured"


@pytest.mark.asyncio
async def test_дошли_отдаёт_версию_и_время(client, db, monkeypatch):
    await _орг(db)
    monkeypatch.setattr(odata_client, "настроено", lambda: True)

    async def дошли():
        return {
            "дошли": True,
            "мс": 412,
            "версия": "3.0",
            "тип_ответа": "application/xml",
        }

    monkeypatch.setattr(odata_client, "проверить_связь", дошли)
    тело = (await client.get(ПРОБА)).json()
    assert тело["дошли"] is True and тело["версия"] == "3.0" and тело["мс"] == 412


@pytest.mark.asyncio
async def test_недоступность_1С_НЕ_роняет_ручку(client, db, monkeypatch):
    """Прибор обязан сообщить результат замера, а не изобразить поломку
    приложения: 200 с «дошли: false», а не 500."""
    await _орг(db)
    monkeypatch.setattr(odata_client, "настроено", lambda: True)

    async def не_дошли():
        return {
            "дошли": False,
            "status": "odata_unavailable",
            "причина": "ConnectTimeout",
        }

    monkeypatch.setattr(odata_client, "проверить_связь", не_дошли)
    ответ = await client.get(ПРОБА)
    assert ответ.status_code == 200
    assert ответ.json()["дошли"] is False


@pytest.mark.asyncio
async def test_в_ответе_нет_ни_логина_ни_пароля_ни_хоста(client, db, monkeypatch):
    """Ответ уходит в браузер администратора и в его переписку."""
    await _орг(db)
    monkeypatch.setenv(
        "ODATA_URL", "https://msk1.example.com/base/odata/standard.odata"
    )
    monkeypatch.setenv("ODATA_LOGIN", "служебный")
    monkeypatch.setenv("ODATA_PASSWORD", "тайна")

    async def дошли():
        return {
            "дошли": True,
            "мс": 10,
            "версия": "3.0",
            "тип_ответа": "application/xml",
        }

    monkeypatch.setattr(odata_client, "проверить_связь", дошли)
    текст = (await client.get(ПРОБА)).text
    assert "тайна" not in текст and "служебный" not in текст
    assert "msk1.example.com" not in текст


@pytest.mark.asyncio
async def test_ручка_ничего_не_пишет_в_1С(client, db, monkeypatch):
    """⚠️ ЗАПИСИ В ЗАХОДЕ ① НЕТ ВОВСЕ, и проверяется это не словом:
    у клиента не должно быть ни одной функции записи."""
    await _орг(db)
    записи = [
        и for и in dir(odata_client) if и.lower() in ("создать", "записать", "провести")
    ]
    assert записи == [], "в клиенте появилась запись: %s" % записи
    monkeypatch.setattr(odata_client, "настроено", lambda: True)

    звали = []

    async def запомнить(путь, **прочее):
        звали.append((путь, прочее.get("метод", "GET")))
        return 200, {"тело": {}, "мс": 1, "версия": "3.0", "тип": "application/xml"}

    monkeypatch.setattr(odata_client, "запросить", запомнить)
    await client.get(ПРОБА)
    assert звали == [("$metadata", "GET")], "проба ходила не туда: %s" % звали
