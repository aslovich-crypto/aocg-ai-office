# -*- coding: utf-8 -*-
"""Тесты общего поиска (T144): чеки и отчёты разом, видимость как у списков."""

from datetime import date

import pytest


def _чек(db, id, org, *, user_id=2, org_id=1, brand=None, legal=None):
    db.receipts.append(
        dict(
            id=id,
            org=org,
            org_brand=brand,
            org_legal=legal,
            date=date(2026, 8, id % 28 + 1),
            amount=100.0 + id,
            org_id=org_id,
            user_id=user_id,
            kkt_fn=None,
            raw_data=None,
            source="manual",
        )
    )


def _отчёт(db, id, title, *, user_id=2, org_id=1, status="Черновик"):
    db.reports.append(
        dict(
            id=id,
            title=title,
            status=status,
            total=500.0,
            created=date(2026, 8, id % 28 + 1),
            org_id=org_id,
            user_id=user_id,
            receiptIds=[],
        )
    )


@pytest.mark.asyncio
async def test_находит_чеки_и_отчёты_разом_без_регистра(client, db, seeded):
    """Человек пишет «ромашка» — в чеке «ООО Ромашка». Регистр не его забота."""
    _чек(db, 101, "ООО Ромашка")
    _отчёт(db, 201, "Командировка в Ромашково")
    r = await client.get("/api/search?q=ромашк")
    assert r.status_code == 200, r.text
    тело = r.json()
    assert [x["id"] for x in тело["receipts"]] == [101]
    assert [x["id"] for x in тело["reports"]] == [201]


@pytest.mark.asyncio
async def test_ищет_и_по_бренду_и_по_юрлицу(client, db, seeded):
    """На чеке видят «Пятёрочка», в юрлице — «Агроторг»: искаться обязаны оба."""
    _чек(db, 102, "Магазин", brand="Пятёрочка", legal='ООО "Агроторг"')
    for запрос in ("пятёроч", "агроторг"):
        r = await client.get(f"/api/search?q={запрос}")
        assert [x["id"] for x in r.json()["receipts"]] == [102], запрос


@pytest.mark.asyncio
async def test_сотрудник_видит_только_своё(client_employee, db, seeded):
    """⚠️ ПОИСК НЕ ИМЕЕТ ПРАВА ПОКАЗЫВАТЬ БОЛЬШЕ, ЧЕМ СПИСКИ (A-ACL).

    Иначе поиск становится дырой: сотрудник не видит чужой чек в списке,
    но находит его запросом."""
    _чек(db, 103, "ООО Ромашка", user_id=2)
    _чек(db, 104, "ООО Ромашка", user_id=999)
    _отчёт(db, 202, "Ромашка отчёт", user_id=2)
    _отчёт(db, 203, "Ромашка чужой", user_id=999)
    r = await client_employee.get("/api/search?q=ромашк")
    тело = r.json()
    assert [x["id"] for x in тело["receipts"]] == [103]
    assert [x["id"] for x in тело["reports"]] == [202]


@pytest.mark.asyncio
async def test_чужая_организация_не_видна_даже_админу(client, db, seeded):
    """org-scope: админ ищет по СВОЕЙ организации, не по всей базе."""
    _чек(db, 105, "ООО Ромашка", org_id=777, user_id=50)
    _отчёт(db, 204, "Ромашка", org_id=777, user_id=50)
    r = await client.get("/api/search?q=ромашк")
    тело = r.json()
    assert тело["receipts"] == [] and тело["reports"] == []


@pytest.mark.asyncio
async def test_короткий_запрос_даёт_пусто_а_не_всё(client, db, seeded):
    """По одной букве совпадает всё подряд — выдача превращается в шум."""
    _чек(db, 106, "ООО Ромашка")
    r = await client.get("/api/search?q=р")
    assert r.status_code == 200
    assert r.json() == {"q": "р", "receipts": [], "reports": []}
