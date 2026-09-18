# -*- coding: utf-8 -*-
"""СЛОВАРЬ КАТЕГОРИЙ = БАЗА, ПРОГОН НА ЖИВОЙ БАЗЕ (Cat).

⚠️ ЗАЧЕМ ЭТОТ ПРИБОР. Механизм словаря сторожит согласие КОПИЙ с источником
(`dict_stamp`, `gen_dictionaries --check`, `check-dictionaries.mjs`).
Ни один из них не смотрит в БАЗУ. Между словарём и строками `categories`
стоит `seed_default_categories`, и он для уже засеянной организации no-op:
существующие строки не обновляются НИКОГДА. Значит правка словаря сама
на прод не доезжает, и увидеть это можно только счётом по базе.

⚠️ ГРАНИЦА: здесь тестовая база. На проде ту же сверку делает владелец —
`scripts/validate_categories_tax_kind.py`, сухим прогоном с бастиона.
"""

import importlib.util
import pathlib

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]


def _рельсы():
    спец = importlib.util.spec_from_file_location(
        "сверка_видов", КОРЕНЬ / "scripts/validate_categories_tax_kind.py"
    )
    м = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(м)
    return м


async def _организация_с_категориями(db):
    from app.categories_seed import seed_default_categories

    await db.добавить_организацию(id=1, name="АОЦГ")
    async with db.pool.acquire() as соединение:
        await seed_default_categories(соединение, 1)


@pytest.mark.asyncio
async def test_все_сорок_восемь_пар_совпадают_со_словарём(db):
    """⚠️ ГЛАВНЫЙ СТОРОЖ: сверяются ВСЕ пары «категория → вид расхода»,
    а не только правленые. Новая категория в словаре, не доехавшая
    до базы, обязана быть видна здесь."""
    from app.dictionaries import DEFAULT_CATEGORIES

    await _организация_с_категориями(db)
    словарь = {имя: вид for _г, статьи in DEFAULT_CATEGORIES for имя, вид in статьи}
    в_базе = {
        з["name"]: з["tax_kind"]
        for з in await db.pool.fetch(
            "SELECT name, tax_kind FROM categories WHERE org_id=1"
        )
    }
    assert len(в_базе) == len(словарь), "в базе %d категорий, в словаре %d" % (
        len(в_базе),
        len(словарь),
    )
    разошлись = {
        имя: (в_базе[имя], вид)
        for имя, вид in словарь.items()
        if в_базе.get(имя) != вид
    }
    assert not разошлись, "база разошлась со словарём: %s" % разошлись


@pytest.mark.asyncio
async def test_у_кейтеринга_и_подарков_новые_виды_расхода(db):
    """Решение владельца 17.09.2026: п. 2 ст. 264 НК для представительских,
    п. 16 ст. 270 НК для подарков."""
    await _организация_с_категориями(db)
    виды = {
        з["name"]: з["tax_kind"]
        for з in await db.pool.fetch(
            "SELECT name, tax_kind FROM categories WHERE org_id=1"
            " AND name IN ('Кейтеринг для встреч', 'Подарки и поощрения')"
        )
    }
    assert виды["Кейтеринг для встреч"] == "Представительские расходы"
    assert виды["Подарки и поощрения"] == "Не учитываемые в целях налогообложения"


@pytest.mark.asyncio
async def test_засев_не_чинит_старое_значение_поэтому_нужен_update(db):
    """⚠️ ЗАМЕР, А НЕ ДОГАДКА. Строка с прежним видом расхода остаётся
    прежней после повторного засева — ровно поэтому на прод нужен UPDATE,
    а не выкат кода."""
    from app.categories_seed import seed_default_categories

    await _организация_с_категориями(db)
    await db.pool.execute(
        "UPDATE categories SET tax_kind='Прочие расходы'"
        " WHERE org_id=1 AND name='Подарки и поощрения'"
    )
    async with db.pool.acquire() as соединение:
        создано = await seed_default_categories(соединение, 1)
    assert создано == 0, "засев тронул уже засеянную организацию"
    осталось = await db.pool.fetchval(
        "SELECT tax_kind FROM categories WHERE org_id=1 AND name='Подарки и поощрения'"
    )
    assert осталось == "Прочие расходы", "засев всё-таки обновил строку"


@pytest.mark.asyncio
async def test_рельсы_видят_расхождение_и_чинят_его_правкой(db, адрес_живой_базы):
    """Сверка обязана покраснеть на расхождении и позеленеть после правки."""
    м = _рельсы()
    await _организация_с_категориями(db)
    await db.pool.execute(
        "UPDATE categories SET tax_kind='Прочие расходы'"
        " WHERE org_id=1 AND name IN ('Кейтеринг для встреч', 'Подарки и поощрения')"
    )
    assert await м.прогнать(адрес_живой_базы, закрепить=False) == 1
    assert await м.прогнать(адрес_живой_базы, закрепить=True) == 0
    виды = {
        з["name"]: з["tax_kind"]
        for з in await db.pool.fetch(
            "SELECT name, tax_kind FROM categories WHERE org_id=1"
            " AND name IN ('Кейтеринг для встреч', 'Подарки и поощрения')"
        )
    }
    assert виды["Кейтеринг для встреч"] == "Представительские расходы"
    assert виды["Подарки и поощрения"] == "Не учитываемые в целях налогообложения"


@pytest.mark.asyncio
async def test_сухая_сверка_ничего_не_пишет(db, адрес_живой_базы):
    """Сухой прогон, поправивший строку, был бы тихой миграцией данных."""
    м = _рельсы()
    await _организация_с_категориями(db)
    await db.pool.execute(
        "UPDATE categories SET tax_kind='Прочие расходы'"
        " WHERE org_id=1 AND name='Подарки и поощрения'"
    )
    await м.прогнать(адрес_живой_базы, закрепить=False)
    осталось = await db.pool.fetchval(
        "SELECT tax_kind FROM categories WHERE org_id=1 AND name='Подарки и поощрения'"
    )
    assert осталось == "Прочие расходы", "сухая сверка изменила базу"
