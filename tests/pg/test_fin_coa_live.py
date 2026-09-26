# -*- coding: utf-8 -*-
"""СПРАВОЧНИК СТАТЕЙ CoA v3.12 (FIN-01) — ПРОГОН НА ЖИВОЙ БАЗЕ.

⚠️ ЗАЧЕМ. `СТАТЬИ` в `app/finance/coa_seed.py` — 94 строки данных, и без
прибора на них это 94 возможности опечататься молча. Семь контрольных чисел
самого справочника-источника (всего 94 · групп 9 · резервов 8 · неденежных 1 ·
income 10 · balance 10 · distribution 17) проверяются здесь ПО БАЗЕ, после
засева, а не по кортежу: между кортежем и таблицей стоит засев, и ломаться
может он.

⚠️ ДОСТУП ПРОВЕРЯЕТСЯ СОСТОЯНИЕМ, А НЕ КОДОМ ОТВЕТА. У второй организации
одна статья переименована меткой — метка обязана НЕ появиться в ответе первой,
а свои 94 статьи обязаны быть на месте (обе половины, правило S-28/S-29).

⚠️ `ping` ОТКРЫТ ОСОЗНАННО (шапка `app/finance/router.py`). Тест на него идёт
БЕЗ подмены смотрящего, и рядом — `coa/` без токена: 401 «Не авторизован»
доказывает, что клиент без подмены действительно не авторизован, иначе
«ping ответил без входа» ничего бы не значило.
"""

from collections import Counter

import pytest
from httpx import ASGITransport, AsyncClient

from app.finance.coa_seed import СТАТЬИ, ВЕРСИЯ, seed_coa_for_org
from app.finance.router import МАРКЕР
from app.finance.schema import init_finance_schema
from app.main import app

ВСЕГО = 94
МЕТКА_ЧУЖОЙ = "ЧУЖАЯ-СТАТЬЯ-ОРГ-2"


async def _засеять(db, org_id):
    async with db.pool.acquire() as соединение:
        return await seed_coa_for_org(соединение, org_id)


async def _число(db, org_id):
    return await db.pool.fetchval(
        "SELECT count(*) FROM fin_coa_articles WHERE org_id=$1", org_id
    )


# ── засев ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_засев_через_init_finance_schema_даёт_94_статьи(db):
    """Путь старта контейнера: `init_db()` → `init_finance_schema` → засев
    каждой организации. Проверяется ИМЕННО он, а не только функция засева."""
    await db.добавить_организацию(1, "АОЦГ")
    await db.добавить_организацию(2, "Вторая")
    async with db.pool.acquire() as соединение:
        await init_finance_schema(соединение)
    assert await _число(db, 1) == ВСЕГО
    assert await _число(db, 2) == ВСЕГО


@pytest.mark.asyncio
async def test_семь_контрольных_чисел_справочника_по_базе(db):
    await db.добавить_организацию(1, "АОЦГ")
    assert await _засеять(db, 1) == ВСЕГО
    с = await db.pool.fetchrow(
        """SELECT count(*)                                   AS всего,
                  count(*) FILTER (WHERE is_group)           AS групп,
                  count(*) FILTER (WHERE is_reserve)         AS резервов,
                  count(*) FILTER (WHERE NOT is_cash)        AS неденежных,
                  count(*) FILTER (WHERE kind='income')      AS income,
                  count(*) FILTER (WHERE kind='balance')     AS balance,
                  count(*) FILTER (WHERE kind='distribution') AS distribution
           FROM fin_coa_articles WHERE org_id=1"""
    )
    assert dict(с) == {
        "всего": 94,
        "групп": 9,
        "резервов": 8,
        "неденежных": 1,
        "income": 10,
        "balance": 10,
        "distribution": 17,
    }


@pytest.mark.asyncio
async def test_неденежная_статья_ровно_3_1_1_12_и_версия_3_12(db):
    await db.добавить_организацию(1, "АОЦГ")
    await _засеять(db, 1)
    неденежные = [
        з["code"]
        for з in await db.pool.fetch(
            "SELECT code FROM fin_coa_articles WHERE org_id=1 AND NOT is_cash"
        )
    ]
    assert неденежные == ["3.1.1.12"]
    версии = {
        з["coa_version"]
        for з in await db.pool.fetch(
            "SELECT DISTINCT coa_version FROM fin_coa_articles WHERE org_id=1"
        )
    }
    assert версии == {"3.12"} == {ВЕРСИЯ}


@pytest.mark.asyncio
async def test_порядок_1_94_и_коды_уникальны(db):
    await db.добавить_организацию(1, "АОЦГ")
    await _засеять(db, 1)
    строки = await db.pool.fetch(
        "SELECT code, position FROM fin_coa_articles WHERE org_id=1 ORDER BY position"
    )
    assert [з["position"] for з in строки] == list(range(1, ВСЕГО + 1))
    assert [з["code"] for з in строки] == [с[0] for с in СТАТЬИ]
    повторы = [к for к, n in Counter(з["code"] for з in строки).items() if n > 1]
    assert not повторы


@pytest.mark.asyncio
async def test_kind_по_правилу_разделов_справочника(db):
    """I → income; II, III, V, VI → expense; IV → 4.1/4.2/4.5 income,
    4.3/4.4/4.6 expense; VII → balance; VIII → distribution."""
    await db.добавить_организацию(1, "АОЦГ")
    await _засеять(db, 1)
    постоянные = {
        "I": "income",
        "II": "expense",
        "III": "expense",
        "V": "expense",
        "VI": "expense",
        "VII": "balance",
        "VIII": "distribution",
    }
    раздел_iv = {"4.1": "income", "4.2": "income", "4.5": "income"}
    раздел_iv.update({"4.3": "expense", "4.4": "expense", "4.6": "expense"})
    разошлись = []
    for з in await db.pool.fetch(
        "SELECT code, section, kind FROM fin_coa_articles WHERE org_id=1"
    ):
        if з["section"] == "IV":
            ждём = раздел_iv.get(".".join(з["code"].split(".")[:2]))
        else:
            ждём = постоянные[з["section"]]
        if з["kind"] != ждём:
            разошлись.append((з["code"], з["section"], з["kind"], ждём))
    assert not разошлись, разошлись


@pytest.mark.asyncio
async def test_повторный_засев_не_дублирует(db):
    """`init_db()` идёт на каждом старте: второй засев — 0 вставок, 94 строки."""
    await db.добавить_организацию(1, "АОЦГ")
    assert await _засеять(db, 1) == ВСЕГО
    assert await _засеять(db, 1) == 0
    async with db.pool.acquire() as соединение:
        await init_finance_schema(соединение)
    assert await _число(db, 1) == ВСЕГО


@pytest.mark.asyncio
async def test_досев_вставляет_только_недостающую_и_не_трогает_остальные(db):
    """Неполный справочник (быстрый выход по счётчику не срабатывает) — засев
    обязан вставить ОДНУ недостающую, не упасть на уникальном индексе
    и не переписать уже лежащие строки."""
    await db.добавить_организацию(1, "АОЦГ")
    await _засеять(db, 1)
    await db.pool.execute(
        "UPDATE fin_coa_articles SET name_ru='ПРАВКА-БЮРО' WHERE org_id=1 AND code='1.1'"
    )
    await db.pool.execute("DELETE FROM fin_coa_articles WHERE org_id=1 AND code='1.2'")
    assert await _засеять(db, 1) == 1
    assert await _число(db, 1) == ВСЕГО
    assert (
        await db.pool.fetchval(
            "SELECT name_ru FROM fin_coa_articles WHERE org_id=1 AND code='1.1'"
        )
        == "ПРАВКА-БЮРО"
    )


# ── GET /api/finance/coa/ ────────────────────────────────────────────────


async def _две_организации(db):
    await db.добавить_организацию(1, "АОЦГ")
    await db.добавить_организацию(2, "Вторая")
    await _засеять(db, 1)
    await _засеять(db, 2)
    await db.pool.execute(
        "UPDATE fin_coa_articles SET name_ru=$1 WHERE org_id=2 AND code='1.1'",
        МЕТКА_ЧУЖОЙ,
    )


@pytest.mark.asyncio
async def test_coa_отдаёт_только_статьи_своей_организации(db, client):
    await _две_организации(db)
    р = await client.get("/api/finance/coa/")
    assert р.status_code == 200, р.text
    тело = р.json()
    assert тело["coa_version"] == ВЕРСИЯ
    assert тело["count"] == ВСЕГО == len(тело["articles"])
    свои = {
        з["id"]
        for з in await db.pool.fetch("SELECT id FROM fin_coa_articles WHERE org_id=1")
    }
    assert {с["id"] for с in тело["articles"]} == свои
    assert МЕТКА_ЧУЖОЙ not in р.text
    # вторая половина: нужное на месте, а не урезано до пустого
    первая = тело["articles"][0]
    assert (первая["code"], первая["position"]) == ("1.1", 1)
    assert первая["name_ru"] == "Генеральное проектирование"
    assert "org_id" not in первая


@pytest.mark.asyncio
async def test_coa_второй_организации_видит_свою_метку(db, as_role):
    """Положительный случай: фильтр не «всегда org 1», а по смотрящему."""
    await _две_организации(db)
    as_role("admin", user_id=5, org_id=2)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            р = await c.get("/api/finance/coa/")
    finally:
        app.dependency_overrides.clear()
    assert р.status_code == 200, р.text
    assert р.json()["count"] == ВСЕГО
    assert МЕТКА_ЧУЖОЙ in р.text


@pytest.mark.asyncio
async def test_coa_незасеянной_организации_пуст(db, client):
    await db.добавить_организацию(1, "АОЦГ")
    await db.добавить_организацию(2, "Вторая")
    await _засеять(db, 2)
    р = await client.get("/api/finance/coa/")
    assert р.status_code == 200, р.text
    assert р.json()["count"] == 0
    assert р.json()["articles"] == []


@pytest.mark.asyncio
async def test_coa_не_отдаёт_деактивированную_статью(db, client):
    await db.добавить_организацию(1, "АОЦГ")
    await _засеять(db, 1)
    await db.pool.execute(
        "UPDATE fin_coa_articles SET is_active=FALSE WHERE org_id=1 AND code='1.1'"
    )
    р = await client.get("/api/finance/coa/")
    коды = [с["code"] for с in р.json()["articles"]]
    assert "1.1" not in коды
    assert len(коды) == ВСЕГО - 1


# ── GET /api/finance/ping ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ping_без_авторизации_отдаёт_маркер_и_coa_без_токена_401(db):
    app.dependency_overrides.clear()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        без_входа = await c.get("/api/finance/coa/")
        р = await c.get("/api/finance/ping")
    # сначала — что клиент действительно без входа, и отказал именно гейт
    assert без_входа.status_code == 401, без_входа.text
    assert без_входа.json()["detail"] == "Не авторизован"
    assert р.status_code == 200, р.text
    assert р.json() == {"module": "finance", "marker": "FIN01-COA-v312"}
    assert МАРКЕР == "FIN01-COA-v312"
