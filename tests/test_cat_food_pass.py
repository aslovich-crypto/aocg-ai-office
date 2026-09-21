# -*- coding: utf-8 -*-
"""Генератор разового прохода флага подтверждения (CAT-FOOD ②, пункт 5).

⚠️ ГЛАВНОЕ, ЧТО СТЕРЕЖЁТСЯ: проход меняет РОВНО ОДНО поле. Перекатегоризация
(Cat-4) заблокирована отдельно — если в SET окажется `category_id`, проход
молча станет ею и разойдётся с документами, уже уехавшими в 1С.
Живая проверка SQL на настоящей базе — в `tests/pg/test_cat_food_live.py`.
"""

import importlib.util
import os
import re

import pytest

_ПУТЬ = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "cat_food_flag_pass.py"
)
_spec = importlib.util.spec_from_file_location("cat_food_flag_pass", _ПУТЬ)
проход = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(проход)

ТАБЛИЦА_PSQL = """\
 id  |               org               |        org_brand         | org_legal |  amount   | category_id | category_manual
-----+---------------------------------+--------------------------+-----------+-----------+-------------+-----------------
  12 | ООО "ЧЕРНЫШЕВСКИЙ"              | Паб Арден                |           | 115984.00 | 8           | f
  18 | ООО "ЧЕРНЫШЕВСКИЙ"              | Ресторан Шушу            |           |  27380.00 | 8           | f
  30 | ООО "Городской супермаркет"     | Супермаркет Азбука Вкуса |           |  14058.00 | 8           | f
  31 | ООО "Городской супермаркет"     | Супермаркет Азбука Вкуса |           |   3054.00 | 8           | f
  40 | ООО ГРИН КИНГ                   | Nothing Fancy            |           |    980.00 | 47          | f
  41 | ООО "Мере"                      | Магазин-кафе Мере        |           |   1520.00 | 14          | t
(6 rows)
"""


def _set_клауза(sql: str) -> str:
    м = re.search(r"UPDATE receipts SET (.+?)\s+WHERE", sql, re.S)
    assert м, "в SQL нет UPDATE receipts SET … WHERE"
    return м.group(1).strip()


def test_разбор_вывода_psql():
    чеки = проход.разобрать(ТАБЛИЦА_PSQL)
    assert [ч["id"] for ч in чеки] == ["12", "18", "30", "31", "40", "41"]
    assert чеки[0]["org_brand"] == "Паб Арден"


def test_флаг_ставится_общепиту_обоим_и_выше_порога():
    итог = {с["id"]: с for с in проход.посчитать(проход.разобрать(ТАБЛИЦА_PSQL))}
    assert итог[12]["подтвердить"] and итог[12]["станет"] == "Не указано"
    assert итог[18]["подтвердить"] and итог[18]["станет"] == "Представительские расходы"
    assert итог[30]["подтвердить"] and итог[30]["станет"] == итог[30]["было"]
    assert not итог[31]["подтвердить"]
    assert not итог[40]["подтвердить"], "слово не ловит — это заход ③, не порог"
    assert итог[41]["подтвердить"] and итог[41]["человек_уже"]


def test_в_set_ровно_одно_поле_флаг():
    sql = проход.собрать_sql({1: [12, 18]})
    assert _set_клауза(sql) == "category_confirm_required = TRUE"


def test_контрольная_сумма_стережёт_категорию_и_время_правки():
    sql = проход.собрать_sql({1: [12]})
    for поле in ("category_id", "category_manual", "amount", "updated_at"):
        assert sql.count(поле) >= 2, "поле %s не входит в контрольную сумму" % поле
    assert "RAISE EXCEPTION" in sql


def test_по_умолчанию_откат_фиксация_только_явно():
    assert проход.собрать_sql({1: [1]}).rstrip().endswith("ROLLBACK;")
    assert проход.собрать_sql({1: [1]}, фиксировать=True).rstrip().endswith("COMMIT;")


def test_проход_ограничен_организацией_и_повтор_безвреден():
    sql = проход.собрать_sql({1: [7, 3]})
    assert (
        "id = ANY(ARRAY[3,7]::int[]) AND org_id = 1 AND category_manual = FALSE" in sql
    )


def test_пустой_список_отказ():
    with pytest.raises(ValueError):
        проход.собрать_sql({})


def test_таблица_без_нужной_колонки_отказ():
    with pytest.raises(ValueError, match="нет колонок"):
        проход.разобрать("id | org | org_brand\n----\n1 | a | b\n")


def test_две_организации_два_update_и_счёт_обязан_сойтись():
    """Выгрузка по всем чекам: у каждого UPDATE своя организация, а итоговая
    проверка роняет транзакцию, если флаг встал не у всех номеров списка —
    номер из чужой организации иначе выпал бы молча."""
    sql = проход.собрать_sql({1: [60, 97], 2: [5]})
    assert "id = ANY(ARRAY[60,97]::int[]) AND org_id = 1 AND" in sql
    assert "id = ANY(ARRAY[5]::int[]) AND org_id = 2 AND" in sql
    assert sql.count("UPDATE receipts SET") == 2
    assert "<> 3 THEN" in sql and "флаг стоит не у всех чеков списка" in sql


def test_выгрузка_с_org_id_разбирается():
    таблица = "id | org_id | org | org_brand | org_legal | amount | category_id | category_manual\n"
    таблица += "---\n5 | 2 | ООО Паб | Паб Арден | | 100 | 8 | f\n"
    итог = проход.посчитать(проход.разобрать(таблица))
    assert итог[0]["org_id"] == 2 and итог[0]["подтвердить"]


def test_ручной_выбор_в_проход_не_попадает():
    """Чек 41 — «Магазин-кафе», человек уже выбрал: правило спросило бы,
    но проход его не трогает вовсе (решение владельца 21.09.2026)."""
    итог = проход.посчитать(проход.разобрать(ТАБЛИЦА_PSQL))
    assert проход.к_проходу(итог, 1) == {1: [12, 18, 30]}


def test_в_условии_update_стоит_страж_ручного_выбора():
    """Второй слой: чек, ставший ручным между выгрузкой и проходом, флаг не
    получит, а сверка счёта уронит транзакцию."""
    sql = проход.собрать_sql({1: [12]})
    assert "AND category_manual = FALSE AND category_confirm_required = FALSE;" in sql
