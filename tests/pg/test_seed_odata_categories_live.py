# -*- coding: utf-8 -*-
"""ЗАСЕВ СООТВЕТСТВИЙ — ПРОГОН НА ЖИВОЙ БАЗЕ (1C-22).

⚠️ ЗАЧЕМ ЖИВОЙ ПРОГОН, ЕСЛИ СПИСОК УЖЕ СВЕРЕН СО СЛОВАРЁМ. Сверка списков
отвечает на вопрос «ничего не забыли»; она НЕ отвечает, ляжет ли засев
в базу и сколько строк получится. Между списком и базой стоит SQL, поиск
по имени и уникальный индекс — проверить их можно только записью.

⚠️ ГРАНИЦА: здесь тестовая база, а не прод. На проде засев выполняет
владелец текстом с бастиона, и сначала сухим прогоном.
"""

import importlib.util
import os
import pathlib

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]


def _засев():
    спец = importlib.util.spec_from_file_location(
        "засев_1с_живой", КОРЕНЬ / "scripts/seed_odata_category_map.py"
    )
    м = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(м)
    return м


async def _организация_с_категориями(db):
    """Организация со ВСЕМИ 48 категориями — тем же засевом, что на проде."""
    from app.categories_seed import seed_default_categories

    await db.добавить_организацию(id=1, name="АОЦГ")
    async with db.pool.acquire() as соединение:
        await seed_default_categories(соединение, 1)
    return await db.pool.fetchval("SELECT count(*) FROM categories WHERE org_id=1")


@pytest.mark.asyncio
async def test_засев_кладёт_сорок_правил_и_восемь_ждут_статью(db, адрес_живой_базы):
    м = _засев()
    всего = await _организация_с_категориями(db)
    assert всего == len(м.СОПОСТАВЛЕНИЕ) + len(м.ЖДУТ_СТАТЬЮ), (
        "в базе %d категорий, а засев знает %d"
        % (всего, len(м.СОПОСТАВЛЕНИЕ) + len(м.ЖДУТ_СТАТЬЮ))
    )

    assert await м.прогнать(адрес_живой_базы, закрепить=True) == 0

    правил = await db.pool.fetchval(
        "SELECT count(*) FROM odata_category_map WHERE org_id=1"
    )
    assert правил == len(м.СОПОСТАВЛЕНИЕ)
    строка = await db.pool.fetchrow(
        "SELECT m.account_code, m.expense_ref, m.expense_name, m.usn_reflection"
        " FROM odata_category_map m JOIN categories c ON c.id = m.category_id"
        " WHERE c.org_id=1 AND c.name='Такси и каршеринг'"
    )
    assert строка["account_code"] == "26"
    assert строка["expense_ref"] == "02d6a734-ca70-11ed-abe2-8812ac539d47"
    assert строка["expense_name"] == "Транспортные расходы"
    assert строка["usn_reflection"] == "Принимаются"

    без_правила = [
        з["name"]
        for з in await db.pool.fetch(
            "SELECT c.name FROM categories c LEFT JOIN odata_category_map m"
            " ON m.category_id = c.id AND m.org_id = c.org_id"
            " WHERE c.org_id=1 AND m.id IS NULL ORDER BY c.name"
        )
    ]
    assert sorted(без_правила) == sorted(имя for имя, _, _ in м.ЖДУТ_СТАТЬЮ), (
        "без правила остались не те категории: %s" % без_правила
    )


@pytest.mark.asyncio
async def test_сухой_прогон_не_оставляет_ни_одной_строки(db, адрес_живой_базы):
    """⚠️ ГЛАВНОЕ РАЗЛИЧИЕ ДВУХ РЕЖИМОВ. Сухой прогон, оставивший данные,
    был бы тихой записью в базу клиента."""
    м = _засев()
    await _организация_с_категориями(db)
    assert await м.прогнать(адрес_живой_базы, закрепить=False) == 0
    осталось = await db.pool.fetchval(
        "SELECT count(*) FROM odata_category_map WHERE org_id=1"
    )
    assert осталось == 0, "сухой прогон записал %d строк" % осталось


@pytest.mark.asyncio
async def test_ненайденное_имя_отменяет_весь_засев(db, адрес_живой_базы):
    """⚠️ ЧАСТИЧНЫЙ ЗАСЕВ ХУЖЕ ОТСУТСТВУЮЩЕГО: часть отчёта уехала бы
    разнесённой, часть нет. Переименуем одну категорию — и не должно лечь
    НИ ОДНОЙ строки, включая те, чьи имена в порядке."""
    м = _засев()
    await _организация_с_категориями(db)
    await db.pool.execute(
        "UPDATE categories SET name='Такси и каршеринг (переименовано)'"
        " WHERE org_id=1 AND name='Такси и каршеринг'"
    )
    assert await м.прогнать(адрес_живой_базы, закрепить=True) == 1
    легло = await db.pool.fetchval(
        "SELECT count(*) FROM odata_category_map WHERE org_id=1"
    )
    assert легло == 0, "засев записал %d строк, хотя одно имя не нашлось" % легло


@pytest.mark.asyncio
async def test_повторный_засев_чинит_расхождение_и_не_плодит_строк(
    db, адрес_живой_базы
):
    """Правило поправили руками не туда — повторный запуск обязан вернуть
    нужное и не завести вторую строку на ту же категорию."""
    м = _засев()
    await _организация_с_категориями(db)
    assert await м.прогнать(адрес_живой_базы, закрепить=True) == 0
    await db.pool.execute(
        "UPDATE odata_category_map SET account_code='20.01', usn_reflection='НеПринимаются'"
        " WHERE category_id = (SELECT id FROM categories WHERE org_id=1"
        "                       AND name='Такси и каршеринг')"
    )
    assert await м.прогнать(адрес_живой_базы, закрепить=True) == 0
    строки = await db.pool.fetch(
        "SELECT m.account_code, m.usn_reflection FROM odata_category_map m"
        " JOIN categories c ON c.id = m.category_id"
        " WHERE c.org_id=1 AND c.name='Такси и каршеринг'"
    )
    assert len(строки) == 1, "повторный засев завёл вторую строку"
    assert строки[0]["account_code"] == "26"
    assert строки[0]["usn_reflection"] == "Принимаются"
    всего = await db.pool.fetchval(
        "SELECT count(*) FROM odata_category_map WHERE org_id=1"
    )
    assert всего == len(м.СОПОСТАВЛЕНИЕ)


def test_файл_засева_лежит_рядом_с_рельсами_миграции():
    """Мелочь, но она стоила захода: скрипт, которого нет по ожидаемому
    пути, владелец не найдёт, а сторож молчит."""
    assert os.path.exists(КОРЕНЬ / "scripts/seed_odata_category_map.py")
