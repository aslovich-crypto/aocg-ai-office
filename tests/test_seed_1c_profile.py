# -*- coding: utf-8 -*-
"""СТОРОЖ ЗАСЕВА ПРОФИЛЯ И ПРАВИЛ ПО ВИДАМ РАСХОДА (1C-22).

⚠️ ЗАЧЕМ СТОРОЖ НА СПИСОК ДАННЫХ. Виды расхода живут в словаре категорий
(`app/dictionaries/categories.json`) и в CHECK у `categories.tax_kind`.
Засев обязан покрывать их ВСЕ — иначе новый вид появится в словаре,
а правила под него не будет, и чеки такой категории молча перестанут
проводиться.

⚠️ ВТОРОЕ: ДВА РЕЖИМА ПЕЧАТИ НЕ ДОЛЖНЫ ПЕРЕПУТАТЬСЯ. Сухой прогон
заканчивается откатом, закрепляющий — ровно одним COMMIT.
"""

import importlib.util
import pathlib
import subprocess
import sys

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]
ОТРАЖЕНИЯ = ("Принимаются", "НеПринимаются", "Распределяются")


def _засев():
    спец = importlib.util.spec_from_file_location(
        "засев_1с", КОРЕНЬ / "scripts/seed_1c_profile.py"
    )
    м = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(м)
    return м


def _виды_словаря():
    sys.path.insert(0, str(КОРЕНЬ))
    from app.dictionaries import DEFAULT_CATEGORIES

    return {вид for _г, статьи in DEFAULT_CATEGORIES for _имя, вид in статьи}


def _напечатать(ключ):
    итог = subprocess.run(
        [sys.executable, "scripts/seed_1c_profile.py", ключ],
        cwd=str(КОРЕНЬ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert итог.returncode == 0, итог.stderr[-800:]
    return итог.stdout


def test_каждый_вид_расхода_словаря_назван_ровно_один_раз():
    """⚠️ ГЛАВНАЯ ПРОВЕРКА: девять видов словаря = засев + «ждут статью»."""
    м = _засев()
    названы = [в for в, *_ in м.ПРАВИЛА_ВИДОВ] + [в for в, *_ in м.ЖДУТ_СТАТЬЮ]
    assert sorted(названы) == sorted(_виды_словаря()), (
        "засев разошёлся со словарём.\n  нет в засеве: %s\n  лишние: %s"
        % (
            sorted(_виды_словаря() - set(названы)),
            sorted(set(названы) - _виды_словаря()),
        )
    )
    assert len(названы) == len(set(названы)), "вид расхода назван дважды"


def test_у_каждого_правила_ссылка_похожа_на_элемент_справочника():
    м = _засев()
    for вид, статья, ссылка, признак, _почему in м.ПРАВИЛА_ВИДОВ:
        assert статья and ссылка, вид
        assert len(ссылка) == 36 and ссылка.count("-") == 4, (
            "в поле ссылки не GUID: %s → %r" % (вид, ссылка)
        )
        assert признак in ОТРАЖЕНИЯ, признак


def test_профиль_аоцг_совпадает_с_решением_владельца():
    """ООО, УСН «доходы минус расходы», НДС в стоимости, счёт 26,
    проведение — когда всё сопоставлено (решение владельца 17.09.2026)."""
    м = _засев()
    assert м.ПРОФИЛЬ == {
        "legal_form": "ooo",
        "tax_regime": "usn_dr",
        "vat_mode": "included",
        "combines_psn": False,
        "default_account_code": "26",
        "auto_post_policy": "when_mapped",
    }


def test_неучитываемые_ждут_статью_с_признаком_непринимаются():
    """⚠️ Иначе расход, который налог НЕ уменьшает, уехал бы принимаемым."""
    м = _засев()
    ждут = {в: п for в, _что, п in м.ЖДУТ_СТАТЬЮ}
    assert ждут["Не учитываемые в целях налогообложения"] == "НеПринимаются"


def test_счёт_в_профиле_не_20_01():
    """20.01 требует номенклатурную группу, которой у нас нет (замер 17.09)."""
    м = _засев()
    assert м.ПРОФИЛЬ["default_account_code"] != "20.01"
    assert "20.01" not in _напечатать("--sql")


def test_сухой_прогон_заканчивается_откатом_и_без_закрепления():
    м = _засев()
    текст = _напечатать("--sql")
    assert "ROLLBACK;" in текст and "COMMIT;" not in текст
    assert текст.count("INSERT INTO org_expense_kind_map") == len(м.ПРАВИЛА_ВИДОВ)
    assert текст.count("INSERT INTO org_accounting_profile") == 1


def test_закрепляющий_режим_называет_себя_и_закрепляет_один_раз():
    текст = _напечатать("--sql-apply")
    assert текст.count("COMMIT;") == 1 and "ROLLBACK;" not in текст
    assert "ЭТО ЗАПИСЬ" in текст


def test_печать_работает_без_драйвера_базы():
    сценарий = (
        "import sys, runpy\n"
        "sys.modules['asyncpg'] = None\n"
        "sys.argv = ['seed_1c_profile.py', '--sql']\n"
        "runpy.run_path('scripts/seed_1c_profile.py', run_name='__main__')\n"
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


def test_засев_не_трогает_правила_по_категориям():
    """Правило по категории — ИСКЛЮЧЕНИЕ, его заводит человек. Засев,
    пишущий исключения, лишает смысла ось видов расхода."""
    текст = _напечатать("--sql")
    assert "org_category_map" not in текст
