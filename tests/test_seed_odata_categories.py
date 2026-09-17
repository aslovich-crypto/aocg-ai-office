# -*- coding: utf-8 -*-
"""СТОРОЖ ЗАСЕВА СООТВЕТСТВИЙ «категория → статья затрат» (1C-22).

⚠️ ЗАЧЕМ СТОРОЖ НА СПИСОК ДАННЫХ. Словарь категорий живёт своей жизнью:
его правят из `app/dictionaries/categories.json`, и новая категория
появится там, а не здесь. Без сверки она просто не попала бы в засев —
молча, и узнали бы об этом на непроведённом документе через месяц.

⚠️ ВТОРОЕ: ДВА РЕЖИМА ПЕЧАТИ НЕ ДОЛЖНЫ ПЕРЕПУТАТЬСЯ. Сухой прогон
обязан заканчиваться откатом, закрепляющий — ровно одним COMMIT.
Перепутанные местами, они означают тихую запись в базу клиента.
"""

import importlib.util
import pathlib
import subprocess
import sys

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
ОТРАЖЕНИЯ = ("Принимаются", "НеПринимаются", "Распределяются", "ВозвратРасхода")


def _засев():
    спец = importlib.util.spec_from_file_location(
        "засев_1с", КОРЕНЬ / "scripts/seed_odata_category_map.py"
    )
    м = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(м)
    return м


def _словарь_категорий():
    sys.path.insert(0, str(КОРЕНЬ))
    from app.dictionaries import DEFAULT_CATEGORIES

    return [имя for _группа, статьи in DEFAULT_CATEGORIES for имя, _вид in статьи]


def test_каждая_категория_словаря_названа_ровно_один_раз():
    """⚠️ ГЛАВНАЯ ПРОВЕРКА: 48 категорий словаря = засев + «ждут статью».
    Ни одной забытой, ни одной дважды."""
    м = _засев()
    названы = [имя for имя, _, _ in м.СОПОСТАВЛЕНИЕ] + [
        имя for имя, _, _ in м.ЖДУТ_СТАТЬЮ
    ]
    словарь = _словарь_категорий()
    assert sorted(названы) == sorted(словарь), (
        "засев разошёлся со словарём категорий.\n  нет в засеве: %s\n  лишние: %s"
        % (
            sorted(set(словарь) - set(названы)),
            sorted(set(названы) - set(словарь)),
        )
    )
    assert len(названы) == len(set(названы)), "категория названа дважды"


def test_у_каждого_правила_есть_статья_и_она_похожа_на_ссылку():
    """Правило без статьи база не примет (expense_ref NOT NULL), а строка
    вида «Прочие затраты» вместо ссылки уехала бы в 1С мусором."""
    м = _засев()
    for имя, статья, ссылка in м.СОПОСТАВЛЕНИЕ:
        assert статья and ссылка, имя
        assert len(ссылка) == 36 and ссылка.count("-") == 4, (
            "в поле ссылки не GUID: %s → %r" % (имя, ссылка)
        )


def test_счёт_один_и_тот_же_и_это_не_20_01():
    """Решение владельца: у АОЦГ счёт 26. Счёт 20.01 требует номенклатурную
    группу, которой у нас нет (замер 17.09.2026)."""
    м = _засев()
    assert м.СЧЁТ == "26"
    assert "20.01" not in _напечатать(м, "--sql")


def test_отражение_в_усн_из_разрешённого_набора():
    м = _засев()
    for _имя, _статья, усн in м.ЖДУТ_СТАТЬЮ:
        assert усн in ОТРАЖЕНИЯ, усн
    assert м.ПРИНИМАЮТСЯ in ОТРАЖЕНИЯ and м.НЕ_ПРИНИМАЮТСЯ in ОТРАЖЕНИЯ


def _напечатать(м, ключ):
    итог = subprocess.run(
        [sys.executable, "scripts/seed_odata_category_map.py", ключ],
        cwd=str(КОРЕНЬ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert итог.returncode == 0, итог.stderr[-800:]
    return итог.stdout


def test_сухой_прогон_заканчивается_откатом_и_без_закрепления():
    м = _засев()
    текст = _напечатать(м, "--sql")
    assert "ROLLBACK;" in текст, "сухой прогон не откатывается"
    assert "COMMIT;" not in текст, "в сухом прогоне есть закрепление"
    assert текст.count("INSERT INTO odata_category_map") == len(м.СОПОСТАВЛЕНИЕ)


def test_закрепляющий_режим_называет_себя_и_закрепляет_один_раз():
    м = _засев()
    текст = _напечатать(м, "--sql-apply")
    assert текст.count("COMMIT;") == 1 and "ROLLBACK;" not in текст
    assert "ЭТО ЗАПИСЬ" in текст, "закрепляющий режим не предупреждает"


def test_печать_работает_без_драйвера_базы():
    """⚠️ Тот же довод, что у рельсов миграции: печать нужна владельцу
    на машине, где `asyncpg` нет и не будет."""
    сценарий = (
        "import sys, runpy\n"
        "sys.modules['asyncpg'] = None\n"
        "sys.argv = ['seed_odata_category_map.py', '--sql']\n"
        "runpy.run_path('scripts/seed_odata_category_map.py', run_name='__main__')\n"
    )
    итог = subprocess.run(
        [sys.executable, "-c", сценарий],
        cwd=str(КОРЕНЬ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert итог.returncode == 0, (итог.stderr or итог.stdout)[-800:]
    assert "BEGIN;" in итог.stdout and "ROLLBACK;" in итог.stdout


def test_вставка_идёт_по_имени_а_не_по_номеру():
    """⚠️ id категорий выдаёт SERIAL: у другой организации порядок иной,
    и номера дали бы чужие проводки молча."""
    м = _засев()
    текст = _напечатать(м, "--sql")
    assert "FROM categories c" in текст and "c.name =" in текст
    assert "VALUES (1, 1," not in текст, "в засеве появился номер категории"
