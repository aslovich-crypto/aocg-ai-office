# -*- coding: utf-8 -*-
"""Тесты общего поиска (T144) — НА ЖИВОЙ БАЗЕ (T36, сессия 2).

ПЕРЕВЕДЕНО С FakePool 03.09.2026. Поиск — М94-класс в чистом виде: зеркала
двойника пришлось учить требовать `org_id=$1` и `user_id=$3` В ТЕКСТЕ
запроса, потому что сами они фильтровали правильно при любом SQL. Здесь
ILIKE и WHERE исполняет PostgreSQL — включая регистр кириллицы, который
двойник подменял питоновским .lower().
"""

from datetime import date

import pytest


async def _чек(db, id, org, *, user_id=2, org_id=1, brand=None, legal=None):
    await db.добавить_чек(
        id=id,
        org=org,
        org_brand=brand,
        org_legal=legal,
        date=date(2026, 8, id % 28 + 1),
        amount=100.0 + id,
        org_id=org_id,
        user_id=user_id,
    )


async def _отчёт(db, id, title, *, user_id=2, org_id=1, status="Черновик"):
    await db.добавить_отчёт(
        id=id,
        title=title,
        status=status,
        total=500.0,
        created=date(2026, 8, id % 28 + 1),
        org_id=org_id,
        user_id=user_id,
    )


@pytest.mark.asyncio
async def test_находит_чеки_и_отчёты_разом_без_регистра(client, db, seeded):
    """Человек пишет «ромашка» — в чеке «ООО Ромашка». Регистр не его забота."""
    await _чек(db, 101, "ООО Ромашка")
    await _отчёт(db, 201, "Командировка в Ромашково")
    r = await client.get("/api/search?q=ромашк")
    assert r.status_code == 200, r.text
    тело = r.json()
    assert [x["id"] for x in тело["receipts"]] == [101]
    assert [x["id"] for x in тело["reports"]] == [201]


@pytest.mark.asyncio
async def test_ищет_и_по_бренду_и_по_юрлицу(client, db, seeded):
    """На чеке видят «Пятёрочка», в юрлице — «Агроторг»: искаться обязаны оба."""
    await _чек(db, 102, "Магазин", brand="Пятёрочка", legal='ООО "Агроторг"')
    for запрос in ("пятёроч", "агроторг"):
        r = await client.get(f"/api/search?q={запрос}")
        assert [x["id"] for x in r.json()["receipts"]] == [102], запрос


@pytest.mark.asyncio
async def test_сотрудник_видит_только_своё(client_employee, db, seeded):
    """⚠️ ПОИСК НЕ ИМЕЕТ ПРАВА ПОКАЗЫВАТЬ БОЛЬШЕ, ЧЕМ СПИСКИ (A-ACL).

    Иначе поиск становится дырой: сотрудник не видит чужой чек в списке,
    но находит его запросом."""
    await db.добавить_пользователя(id=999, first_name="Коллега", role="employee")
    await _чек(db, 103, "ООО Ромашка", user_id=2)
    await _чек(db, 104, "ООО Ромашка", user_id=999)
    await _отчёт(db, 202, "Ромашка отчёт", user_id=2)
    await _отчёт(db, 203, "Ромашка чужой", user_id=999)
    r = await client_employee.get("/api/search?q=ромашк")
    тело = r.json()
    assert [x["id"] for x in тело["receipts"]] == [103]
    assert [x["id"] for x in тело["reports"]] == [202]


@pytest.mark.asyncio
async def test_чужая_организация_не_видна_даже_админу(client, db, seeded):
    """org-scope: админ ищет по СВОЕЙ организации, не по всей базе."""
    await db.добавить_пользователя(id=50, first_name="Чужой", role="admin", org_id=777)
    await _чек(db, 105, "ООО Ромашка", org_id=777, user_id=50)
    await _отчёт(db, 204, "Ромашка", org_id=777, user_id=50)
    r = await client.get("/api/search?q=ромашк")
    тело = r.json()
    assert тело["receipts"] == [] and тело["reports"] == []


@pytest.mark.asyncio
async def test_короткий_запрос_даёт_пусто_а_не_всё(client, db, seeded):
    """По одной букве совпадает всё подряд — выдача превращается в шум."""
    await _чек(db, 106, "ООО Ромашка")
    r = await client.get("/api/search?q=р")
    assert r.status_code == 200
    assert r.json() == {"q": "р", "receipts": [], "reports": []}
