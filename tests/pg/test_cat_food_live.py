# -*- coding: utf-8 -*-
"""CAT-FOOD ② на живой базе: флаг при создании, порог, отказ выгрузки, «документ
устарел», миграции на НЕПУСТЫХ таблицах.

⚠️ НАСТОЯЩАЯ 1С НЕ УЧАСТВУЕТ: подделка площадки и настройка отчёта берутся
из `test_odata_export_live.py` импортом, а не копией — две копии поддельной
1С разошлись бы, и одна из них начала бы уметь больше оригинала.

⚠️ МИГРАЦИИ ПРОВЕРЯЮТСЯ НА ТАБЛИЦЕ С ДАННЫМИ И ТЕМ ЖЕ `init_db()`, ЧТО НА СТАРТЕ.
Урок 11.09.2026 (журнал улик): DDL в `init_db()` идёт по ПУСТОЙ таблице, и порчу
данных миграцией живой контур не видел. Урок 13а.24: поле внутри CREATE TABLE
существующей таблицы не появляется — проверяется таблицей, которая УЖЕ есть.
"""

import pytest

from app import odata_client
from app.database import init_db
from tests.pg.test_odata_export_live import (  # noqa: F401 — фикстура `настроено`
    ВЫГРУЗКА,
    ПоддельнаяОдинЭс,
    _профиль,
    _отчёт,
    настроено,
)

ЧЕКИ = "/api/receipts/"


async def _статьи(db):
    """Три статьи организации 1, включая ФОЛБЭК: без него «записано пустотой»
    не отличить от «фолбэка не было» — заведомо разная пара."""
    await db.добавить_организацию(id=1, name="АОЦГ")
    await db.обеспечить_пользователя(id=1, first_name="Админ", role="admin")
    группа = await db.pool.fetchval(
        "INSERT INTO category_groups (org_id, name, position) VALUES (1, 'Общие', 1)"
        " RETURNING id"
    )
    номера = {}
    for место, имя in enumerate(
        ("Представительские расходы", "Прочие хозрасходы", "Продукты для офиса"), 1
    ):
        номера[имя] = await db.pool.fetchval(
            "INSERT INTO categories (org_id, group_id, name, tax_kind, position)"
            " VALUES (1, $1, $2, 'Прочие расходы', $3) RETURNING id",
            группа,
            имя,
            место,
        )
    return номера


async def _создать(client, org, amount):
    ответ = await client.post(
        ЧЕКИ, json={"date": "2026-09-20", "org": org, "amount": amount}
    )
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


async def _флаг(db, номер):
    return await db.pool.fetchval(
        "SELECT category_confirm_required FROM receipts WHERE id=$1", номер
    )


# ── ФЛАГ ПРИ СОЗДАНИИ (Р2, Р3, находка В) ───────────────────────────────────


@pytest.mark.asyncio
async def test_общепит_получает_флаг_при_любой_сумме(client, db):
    """М4. Сто рублей в пабе — всё равно вопрос человеку."""
    await _статьи(db)
    чек = await _создать(client, "Паб Арден", 100)
    assert await _флаг(db, чек["id"]) is True


@pytest.mark.asyncio
async def test_флаг_ставится_и_когда_категорию_прислал_фронт(client, db):
    """Находка В: фронт присылает подсказку сервера в теле, и categorize
    при создании не зовётся. Флаг от этого не зависит."""
    номера = await _статьи(db)
    ответ = await client.post(
        ЧЕКИ,
        json={
            "date": "2026-09-20",
            "org": "Паб Арден",
            "amount": 100,
            "category": "Представительские расходы",
        },
    )
    чек = ответ.json()
    assert await _флаг(db, чек["id"]) is True
    assert чек["category_id"] == номера["Представительские расходы"]


@pytest.mark.asyncio
async def test_общепит_без_статьи_записан_пустотой_а_не_фолбэком(client, db):
    """Д2. Пара: не опознанный продавец получает фолбэк, как сегодня, — значит
    фолбэк в организации есть, и пустота у паба не случайна."""
    номера = await _статьи(db)
    паб = await _создать(client, "Паб Арден", 100)
    # другая сумма: одинаковая за 90 секунд — это защита от двойного нажатия
    ромашка = await _создать(client, "ООО Ромашка", 101)
    assert паб["category_id"] is None
    assert ромашка["category_id"] == номера["Прочие хозрасходы"]


@pytest.mark.asyncio
async def test_ресторан_получает_статью_из_названия(client, db):
    номера = await _статьи(db)
    чек = await _создать(client, "Ресторан Шушу", 27380)
    assert чек["category_id"] == номера["Представительские расходы"]
    assert await _флаг(db, чек["id"]) is True


@pytest.mark.asyncio
async def test_чек_дороже_порога_получает_флаг(client, db):
    """М5, первая половина. Профиля нет — порог по умолчанию, 10 000 ₽."""
    await _статьи(db)
    дорогой = await _создать(client, "Магнит", 12000)
    дешёвый = await _создать(client, "Магнит", 9000)
    assert await _флаг(db, дорогой["id"]) is True
    assert await _флаг(db, дешёвый["id"]) is False


@pytest.mark.asyncio
async def test_порог_берётся_из_профиля_организации(client, db):
    """М6. Порог — настройка клиента, а не константа кода: 5 000 спрашивает
    у 6 000 ₽, NULL не спрашивает и у миллиона."""
    await _статьи(db)
    await _профиль(db)
    await db.pool.execute(
        "UPDATE org_accounting_profile SET confirm_amount_threshold = 5000 WHERE org_id = 1"
    )
    assert await _флаг(db, (await _создать(client, "Магнит", 6000))["id"]) is True
    await db.pool.execute(
        "UPDATE org_accounting_profile SET confirm_amount_threshold = NULL WHERE org_id = 1"
    )
    assert await _флаг(db, (await _создать(client, "Магнит", 1_000_000))["id"]) is False


@pytest.mark.asyncio
async def test_флаг_доезжает_до_фронта_в_ответах_чека(client, db):
    """М13. Признак, которого нет в ответе, для экрана не существует (Р4 —
    следующим заходом во фронте; дорога к нему — здесь)."""
    await _статьи(db)
    чек = await _создать(client, "Паб Арден", 100)
    один = (await client.get(ЧЕКИ + str(чек["id"]))).json()
    список = (await client.get(ЧЕКИ)).json()
    assert один["category_confirm_required"] is True
    assert один["category_manual"] is False
    assert [ч["category_confirm_required"] for ч in список if ч["id"] == чек["id"]] == [
        True
    ]


# ── ПОДТВЕРЖДЕНИЕ ЧЕЛОВЕКОМ ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_правка_на_не_указано_отвергается_и_ничего_не_меняет(client, db):
    """Д2: иначе подтверждение молча превратило бы «не знаю» в фолбэк."""
    await _статьи(db)
    чек = await _создать(client, "Паб Арден", 100)
    ответ = await client.patch(ЧЕКИ + str(чек["id"]), json={"category": "Не указано"})
    assert ответ.status_code == 422
    строка = await db.pool.fetchrow(
        "SELECT category_id, category_manual FROM receipts WHERE id=$1", чек["id"]
    )
    assert строка["category_id"] is None and строка["category_manual"] is False


@pytest.mark.asyncio
async def test_выбор_статьи_человеком_подтверждает(client, db):
    номера = await _статьи(db)
    чек = await _создать(client, "Паб Арден", 100)
    ответ = await client.patch(
        ЧЕКИ + str(чек["id"]), json={"category": "Представительские расходы"}
    )
    assert ответ.status_code == 200
    assert ответ.json()["category_manual"] is True
    assert ответ.json()["category_id"] == номера["Представительские расходы"]


# ── Р5: ВЫГРУЗКА ОТКАЗЫВАЕТ, ПОКА НЕ ПОДТВЕРЖДЕНО ──────────────────────────


async def _паб_в_отчёте(db):
    await _отчёт(db)
    await db.pool.execute(
        "UPDATE receipts SET org_brand = 'Паб Арден', amount = 115984,"
        " category_confirm_required = TRUE WHERE id = 1"
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("настроено")
async def test_неподтверждённый_чек_не_уезжает_и_отказ_бесплатный(
    client, db, monkeypatch
):
    """М7 и М8. Отказ называет чек с суммой и не стоит ничего: ни одного
    запроса в 1С, ни строки в журнале выгрузок."""
    await _паб_в_отчёте(db)
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 409
    текст = ответ.json()["detail"]
    assert "Чек №1" in текст and "Паб Арден" in текст and "115 984,00" in текст
    assert одинэс.вызовы == [], "до отказа ходили в 1С"
    assert await db.pool.fetchval("SELECT count(*) FROM odata_exports") == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("настроено")
async def test_после_подтверждения_выгрузка_идёт(client, db, monkeypatch):
    """Положительная пара: без неё сломанная для всех выгрузка читалась бы
    как работающий отказ."""
    await _паб_в_отчёте(db)
    await db.pool.execute("UPDATE receipts SET category_manual = TRUE WHERE id = 1")
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 200, ответ.text
    assert ответ.json()["выгружен"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("настроено")
async def test_чек_дороже_порога_из_ручки_не_уезжает(client, db, monkeypatch):
    """М5 целиком: флаг от порога поставлен РУЧКОЙ создания, выгрузка его видит."""
    await _отчёт(db)
    чек = await _создать(client, "Магнит", 12000)
    await db.pool.execute(
        "INSERT INTO report_items (report_id, receipt_id) VALUES (1, $1)", чек["id"]
    )
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 409
    assert "Чек №%d" % чек["id"] in ответ.json()["detail"]


# ── Р6: ДОКУМЕНТ В 1С УСТАРЕЛ ───────────────────────────────────────────────


async def _выгружен(client, db, monkeypatch):
    await _отчёт(db)
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    assert (await client.post(ВЫГРУЗКА % 1)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.usefixtures("настроено")
async def test_правка_чека_после_выгрузки_устаревает_документ(client, db, monkeypatch):
    await _выгружен(client, db, monkeypatch)
    ответ = await client.patch(ЧЕКИ + "1", json={"category": "Продукты для офиса"})
    assert ответ.status_code == 200, ответ.text
    повтор = (await client.post(ВЫГРУЗКА % 1)).json()["detail"]
    assert "устарел" in повтор and "№1" in повтор


@pytest.mark.asyncio
@pytest.mark.usefixtures("настроено")
async def test_правка_прямым_sql_тоже_видна(client, db, monkeypatch):
    """Д3: ради этого время двигает триггер базы, а не ручка."""
    await _выгружен(client, db, monkeypatch)
    await db.pool.execute("UPDATE receipts SET amount = 999 WHERE id = 1")
    assert "устарел" in (await client.post(ВЫГРУЗКА % 1)).json()["detail"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("настроено")
async def test_флаг_и_отметка_человека_время_не_двигают(client, db, monkeypatch):
    """М10. Иначе разовый проход «состарил» бы все выгруженные документы."""
    await _выгружен(client, db, monkeypatch)
    до = await db.pool.fetchval("SELECT updated_at FROM receipts WHERE id = 1")
    await db.pool.execute(
        "UPDATE receipts SET category_confirm_required = TRUE, category_manual = TRUE,"
        " photo_key = 'снимок' WHERE id = 1"
    )
    после = await db.pool.fetchval("SELECT updated_at FROM receipts WHERE id = 1")
    assert после == до
    assert "устарел" not in (await client.post(ВЫГРУЗКА % 1)).json()["detail"]


# ── МИГРАЦИИ НА ТАБЛИЦАХ, КОТОРЫЕ УЖЕ ЕСТЬ ──────────────────────────────────


@pytest.mark.asyncio
async def test_updated_at_у_старых_чеков_равен_created_at(db):
    """М9. `NOT NULL DEFAULT NOW()` одной строкой проставил бы момент миграции
    ВСЕМ старым чекам — и каждый выгруженный документ стал бы «устаревшим»."""
    from datetime import date, datetime

    await db.добавить_организацию(id=1)
    await db.добавить_чек(
        id=1,
        org="Старый",
        amount=1,
        date=date(2026, 1, 1),
        created_at=datetime(2026, 1, 1, 12, 0),
    )
    await db.pool.execute("ALTER TABLE receipts DROP COLUMN updated_at")
    await init_db()
    assert (
        await db.pool.fetchval(
            "SELECT updated_at = created_at FROM receipts WHERE id = 1"
        )
        is True
    )


@pytest.mark.asyncio
async def test_порог_появляется_у_уже_существующего_профиля(db):
    """М12, урок 13а.24. Профиль заведён ДО миграции, как на проде с 1C-22."""
    await db.добавить_организацию(id=1)
    await _профиль(db)
    await db.pool.execute(
        "ALTER TABLE org_accounting_profile DROP COLUMN confirm_amount_threshold"
    )
    await init_db()
    assert (
        await db.pool.fetchval(
            "SELECT confirm_amount_threshold FROM org_accounting_profile WHERE org_id = 1"
        )
        == 10000
    )


# ── РАЗОВЫЙ ПРОХОД: СГЕНЕРИРОВАННЫЙ SQL НА НАСТОЯЩЕЙ БАЗЕ ───────────────────


def _генератор():
    import importlib.util
    import os

    путь = os.path.join(
        os.path.dirname(__file__), "..", "..", "scripts", "cat_food_flag_pass.py"
    )
    spec = importlib.util.spec_from_file_location("cat_food_flag_pass", путь)
    модуль = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(модуль)
    return модуль


async def _два_чека(db):
    from datetime import date

    await db.добавить_организацию(id=1)
    await db.добавить_организацию(id=2, name="Чужая")
    await db.добавить_чек(id=1, org="Паб", amount=100, date=date(2026, 9, 1), org_id=1)
    await db.добавить_чек(id=2, org="Бар", amount=200, date=date(2026, 9, 2), org_id=2)


@pytest.mark.asyncio
async def test_проход_ставит_только_флаг(db):
    """Флаг встаёт у списка; категория, отметка и время правки — прежние."""
    await _два_чека(db)
    до = await db.pool.fetch(
        "SELECT id, category_id, category_manual, updated_at FROM receipts ORDER BY id"
    )
    sql = _генератор().собрать_sql({1: [1], 2: [2]}, фиксировать=True)
    async with db.pool.acquire() as соединение:
        await соединение.execute(sql)
    после = await db.pool.fetch(
        "SELECT id, category_id, category_manual, updated_at FROM receipts ORDER BY id"
    )
    assert [dict(с) for с in после] == [dict(с) for с in до]
    assert (
        await db.pool.fetchval(
            "SELECT count(*) FROM receipts WHERE category_confirm_required"
        )
        == 2
    )


@pytest.mark.asyncio
async def test_проход_с_чужим_номером_откатывается_целиком(db):
    """Номер 2 принадлежит организации 2, а в списке стоит под 1: счёт не
    сходится — исключение, и не встаёт НИ ОДИН флаг, включая верный."""
    import asyncpg

    await _два_чека(db)
    sql = _генератор().собрать_sql({1: [1, 2]}, фиксировать=True)
    async with db.pool.acquire() as соединение:
        with pytest.raises(asyncpg.RaiseError, match="не у всех чеков"):
            await соединение.execute(sql)
        await соединение.execute("ROLLBACK")
    assert (
        await db.pool.fetchval(
            "SELECT count(*) FROM receipts WHERE category_confirm_required"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_проход_не_трогает_ручной_выбор(db):
    """Решение человека неприкосновенно: ручной номер, попавший в список
    (чек стал ручным между выгрузкой и проходом), флаг не получает — и
    сверка счёта роняет всю транзакцию, а не молчит."""
    import asyncpg

    await _два_чека(db)
    await db.pool.execute("UPDATE receipts SET category_manual = TRUE WHERE id = 1")
    sql = _генератор().собрать_sql({1: [1]}, фиксировать=True)
    async with db.pool.acquire() as соединение:
        with pytest.raises(asyncpg.RaiseError, match="не у всех чеков"):
            await соединение.execute(sql)
        await соединение.execute("ROLLBACK")
    assert (
        await db.pool.fetchval(
            "SELECT category_confirm_required FROM receipts WHERE id = 1"
        )
        is False
    )
