# -*- coding: utf-8 -*-
"""ЗАСЕВ ПРОФИЛЯ И ПРАВИЛ — ПРОГОН НА ЖИВОЙ БАЗЕ (1C-22).

⚠️ ЗАЧЕМ ЖИВОЙ ПРОГОН, ЕСЛИ СПИСОК УЖЕ СВЕРЕН СО СЛОВАРЁМ. Сверка списков
отвечает «ничего не забыли»; она НЕ отвечает, ляжет ли засев в базу
и сколько строк получится. Между списком и базой стоят CHECK, уникальный
индекс и связки профиля — проверить их можно только записью.

⚠️ ГРАНИЦА: здесь тестовая база, а не прод. На проде засев выполняет
владелец текстом с бастиона, и сначала сухим прогоном.
"""

import importlib.util
import pathlib

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]


def _засев():
    спец = importlib.util.spec_from_file_location(
        "засев_1с_живой", КОРЕНЬ / "scripts/seed_1c_profile.py"
    )
    м = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(м)
    return м


async def _организация_с_категориями(db):
    """Организация со ВСЕМИ 48 категориями — тем же засевом, что на проде."""
    from app.categories_seed import seed_default_categories

    await db.добавить_организацию(id=1, name="АОЦГ")
    # ⚠️ РЕЖИМ В КАРТОЧКЕ — ЧАСТЬ ПРЕДПОСЫЛОК: засев сверяет его с профилем.
    await db.pool.execute("UPDATE organizations SET tax_system='usn_dr' WHERE id=1")
    async with db.pool.acquire() as соединение:
        await seed_default_categories(соединение, 1)


@pytest.mark.asyncio
async def test_засев_кладёт_профиль_и_шесть_правил(db, адрес_живой_базы):
    м = _засев()
    await _организация_с_категориями(db)
    assert await м.прогнать(адрес_живой_базы, закрепить=True) == 0

    профиль = await db.pool.fetchrow(
        "SELECT legal_form, tax_regime, vat_mode, combines_psn,"
        " default_account_code, auto_post_policy FROM org_accounting_profile"
        " WHERE org_id=1"
    )
    assert dict(профиль) == м.ПРОФИЛЬ

    правил = await db.pool.fetchval(
        "SELECT count(*) FROM org_expense_kind_map WHERE org_id=1"
    )
    assert правил == len(м.ПРАВИЛА_ВИДОВ) == 9
    переопределений = await db.pool.fetchval(
        "SELECT count(*) FROM org_category_map WHERE org_id=1"
    )
    assert переопределений == len(м.ПЕРЕОПРЕДЕЛЕНИЯ) == 34
    строка = await db.pool.fetchrow(
        "SELECT expense_ref, expense_name, usn_reflection, account_code"
        " FROM org_expense_kind_map WHERE org_id=1 AND tax_kind='Транспортные расходы'"
    )
    assert строка["expense_ref"] == "02d6a734-ca70-11ed-abe2-8812ac539d47"
    assert строка["usn_reflection"] == "Принимаются"
    assert строка["account_code"] is None, "счёт вида должен браться из профиля"


@pytest.mark.asyncio
async def test_виды_без_правила_это_ровно_те_что_ждут_статей(db, адрес_живой_базы):
    """⚠️ ГЛАВНАЯ СВЯЗКА ЗАСЕВА С РЕАЛЬНОСТЬЮ: не сопоставленными обязаны
    остаться ровно три вида, под которые статей в 1С нет."""
    м = _засев()
    await _организация_с_категориями(db)
    await м.прогнать(адрес_живой_базы, закрепить=True)
    без_правила = [
        з["tax_kind"]
        for з in await db.pool.fetch(
            "SELECT DISTINCT c.tax_kind FROM categories c"
            " LEFT JOIN org_expense_kind_map m"
            "   ON m.tax_kind = c.tax_kind AND m.org_id = c.org_id"
            " WHERE c.org_id=1 AND m.id IS NULL ORDER BY 1"
        )
    ]
    assert sorted(без_правила) == sorted(в for в, _, _ in м.ЖДУТ_СТАТЬЮ)


@pytest.mark.asyncio
async def test_сухой_прогон_не_оставляет_ни_одной_строки(db, адрес_живой_базы):
    """⚠️ Сухой прогон, оставивший данные, был бы тихой записью."""
    м = _засев()
    await _организация_с_категориями(db)
    assert await м.прогнать(адрес_живой_базы, закрепить=False) == 0
    assert await db.pool.fetchval("SELECT count(*) FROM org_accounting_profile") == 0
    assert await db.pool.fetchval("SELECT count(*) FROM org_expense_kind_map") == 0
    assert await db.pool.fetchval("SELECT count(*) FROM org_category_map") == 0


@pytest.mark.asyncio
async def test_без_организации_засев_отказывается(db, адрес_живой_базы):
    """Организации нет — писать некуда, и молчаливый ноль строк был бы
    неотличим от успешного засева."""
    м = _засев()
    assert await м.прогнать(адрес_живой_базы, закрепить=True) == 1
    assert await db.pool.fetchval("SELECT count(*) FROM org_expense_kind_map") == 0
    assert await db.pool.fetchval("SELECT count(*) FROM org_category_map") == 0


@pytest.mark.asyncio
async def test_повторный_засев_чинит_расхождение_и_не_плодит_строк(
    db, адрес_живой_базы
):
    """Правило поправили руками не туда — повторный запуск возвращает
    нужное и не заводит вторую строку на тот же вид."""
    м = _засев()
    await _организация_с_категориями(db)
    assert await м.прогнать(адрес_живой_базы, закрепить=True) == 0
    await db.pool.execute(
        "UPDATE org_expense_kind_map SET expense_ref='чужая', usn_reflection='НеПринимаются'"
        " WHERE org_id=1 AND tax_kind='Прочие расходы'"
    )
    await db.pool.execute(
        "UPDATE org_accounting_profile SET default_account_code='20.01' WHERE org_id=1"
    )
    assert await м.прогнать(адрес_живой_базы, закрепить=True) == 0
    строки = await db.pool.fetch(
        "SELECT expense_ref, usn_reflection FROM org_expense_kind_map"
        " WHERE org_id=1 AND tax_kind='Прочие расходы'"
    )
    assert len(строки) == 1
    assert строки[0]["expense_ref"] == "4c476c83-096c-11e9-80ed-0050569f5448"
    assert строки[0]["usn_reflection"] == "Принимаются"
    счёт = await db.pool.fetchval(
        "SELECT default_account_code FROM org_accounting_profile WHERE org_id=1"
    )
    assert счёт == "26", "повторный засев не вернул счёт профиля"


@pytest.mark.asyncio
async def test_расхождение_режима_отменяет_засев(db, адрес_живой_базы):
    """⚠️ РЕЖИМ ЖИВЁТ В ДВУХ МЕСТАХ. Разойдись они — экран «Сводка» считает
    налог по одному режиму, а документ в 1С уезжает по другому, и увидеть
    это некому. Засев отказывается целиком."""
    м = _засев()
    await _организация_с_категориями(db)
    await db.pool.execute("UPDATE organizations SET tax_system='osno' WHERE id=1")
    assert await м.прогнать(адрес_живой_базы, закрепить=True) == 1
    assert await db.pool.fetchval("SELECT count(*) FROM org_accounting_profile") == 0
    assert await db.pool.fetchval("SELECT count(*) FROM org_expense_kind_map") == 0
    assert await db.pool.fetchval("SELECT count(*) FROM org_category_map") == 0


@pytest.mark.asyncio
async def test_пустой_режим_в_карточке_тоже_расхождение(db, адрес_живой_базы):
    """NULL — это «не знаем», а не «совпадает»."""
    м = _засев()
    await _организация_с_категориями(db)
    await db.pool.execute("UPDATE organizations SET tax_system=NULL WHERE id=1")
    assert await м.прогнать(адрес_живой_базы, закрепить=True) == 1
