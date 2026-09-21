# -*- coding: utf-8 -*-
"""CAT-FOOD ②: разовый проход флага «категорию подтверждает человек» по старым чекам.

ЗАПУСК (на машине разработчика, НЕ на проде):
    ./venv/bin/python scripts/cat_food_flag_pass.py таблица.txt            # таблица + SQL с ROLLBACK
    ./venv/bin/python scripts/cat_food_flag_pass.py таблица.txt --commit   # тот же SQL с COMMIT

`таблица.txt` — вывод psql владельца по чекам (колонки id, org, org_brand,
org_legal, amount, category_id, category_manual; разделитель «|» или табуляция).

⚠️ ПОЧЕМУ ЗДЕСЬ, А НЕ В init_db И НЕ НА СЕРВЕРЕ. Правила — питон
(`app/categorization.py`), на площадке нет консоли, на бастионе нет кода (T85).
Поэтому список чеков считается ЗДЕСЬ теми же функциями, что у приложения,
а на бастион уезжает готовый SQL с явным списком номеров.

⚠️ ЧЕМ ГАРАНТИРОВАНО, ЧТО КАТЕГОРИЯ НЕ МЕНЯЕТСЯ:
  ① в SET ровно одно поле — флаг; это стережёт тест генератора;
  ② контрольная сумма по id, category_id, category_manual, amount, updated_at
    ДО и ПОСЛЕ в одной транзакции; разошлась — исключение, транзакция
    откатывается сама, и COMMIT после неё уже ничего не зафиксирует;
  ③ WHERE ограничен списком номеров, организацией, «человек не выбирал»
    и «флаг ещё не стоит» — повторный прогон не меняет ничего, а ручной выбор
    не трогается вовсе (его нет и в списке — см. `к_проходу`).
Триггер времени правки флаг не видит (его нет в условии триггера), поэтому
выгруженные документы проход «состарить» не может — это и проверяет ② через
updated_at.
"""

import argparse
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.categorization import (  # noqa: E402
    НЕ_УКАЗАНО,
    ОБЩЕПИТ,
    categorize,
    нужно_подтверждение,
    тип_продавца,
)

ПОРОГ = 10000
# Номера статей организации 1 из замера владельца 21.09.2026 — только для
# печати «было» словами; на расчёт флага не влияют.
СТАТЬИ_ОРГ_1 = {
    "14": "Представительские расходы",
    "8": "Продукты для офиса",
    "47": "Прочие хозрасходы",
    "3": "Хозтовары и инвентарь",
    "25": "IT-оборудование (мелкое)",
    "48": "Не учитываемые в налоговом учёте",
}
КОЛОНКИ = (
    "id",
    "org",
    "org_brand",
    "org_legal",
    "amount",
    "category_id",
    "category_manual",
)

_ИСТИНА = {"t", "true", "да", "1"}


def разобрать(текст: str) -> list:
    """Вывод psql (или TSV) → список словарей. Шапка ищется по колонке `org_brand`."""
    строки = [с for с in текст.splitlines() if с.strip()]
    раздел = (
        "\t"
        if any("\t" in с for с in строки) and not any("|" in с for с in строки)
        else "|"
    )
    шапка, данные = None, []
    for с in строки:
        if re.fullmatch(r"[\s\-+|]+", с) or re.fullmatch(
            r"\(\d+ (rows?|строк\w*)\)", с.strip()
        ):
            continue
        ячейки = [я.strip() for я in с.split(раздел)]
        if шапка is None:
            if "org_brand" in ячейки and "id" in ячейки:
                шапка = ячейки
            continue
        if len(ячейки) != len(шапка):
            raise ValueError("строка не совпадает с шапкой по числу ячеек: %r" % с[:80])
        данные.append(dict(zip(шапка, ячейки)))
    if шапка is None:
        raise ValueError("не нашёл шапку с колонками id и org_brand")
    нет = [к for к in КОЛОНКИ if к not in шапка]
    if нет:
        raise ValueError("в таблице нет колонок: %s" % ", ".join(нет))
    return данные


def посчитать(чеки, порог=ПОРОГ) -> list:
    итог = []
    for ч in чеки:
        сумма = float((ч["amount"] or "0").replace(" ", "").replace(",", "."))
        орг, вывеска = ч["org"] or "", ч["org_brand"] or None
        тип = тип_продавца(вывеска, орг)
        было = СТАТЬИ_ОРГ_1.get(ч["category_id"], ч["category_id"] or НЕ_УКАЗАНО)
        # ⚠️ «СТАНЕТ» СЧИТАЕТСЯ ТОЛЬКО У ОБЩЕПИТА: только у него меняется слой.
        # У остальных категория по построению прежняя, а позиций в таблице нет.
        станет = categorize(орг, [], brand=вывеска) if тип == ОБЩЕПИТ else было
        итог.append(
            {
                "id": int(ч["id"]),
                # org_id в выгрузке по отчёту 9 не было (там одна организация);
                # в выгрузке по всем чекам он обязан быть — см. main().
                "org_id": int(ч["org_id"]) if ч.get("org_id") else None,
                "вывеска": вывеска or орг,
                "тип": тип,
                "было": было,
                "станет": станет,
                "сумма": сумма,
                "подтвердить": нужно_подтверждение(орг, вывеска, сумма, порог),
                "человек_уже": (ч["category_manual"] or "").lower() in _ИСТИНА,
            }
        )
    return итог


def к_проходу(итог, org_по_умолчанию: int) -> dict:
    """{org_id: [номера]} для прохода: правило требует подтверждения И человек
    ещё не выбирал.

    ⚠️ РУЧНОЙ ВЫБОР ПРОХОД НЕ ТРОГАЕТ ВОВСЕ (решение владельца 21.09.2026).
    Отметка `category_manual` — решение человека, и среди них есть общепит
    со статьёй, которую машина не предложила бы (чек 23: паб → хозтовары).
    Флаг им не нужен: выгрузку держит только «требует И не подтверждён»."""
    по_организациям: dict = {}
    for с in итог:
        if с["подтвердить"] and not с["человек_уже"]:
            по_организациям.setdefault(с["org_id"] or org_по_умолчанию, []).append(
                с["id"]
            )
    return по_организациям


# Контрольная сумма всего, что проход НЕ имеет права менять.
_СУММА = (
    "md5(string_agg(id || '|' || coalesce(category_id::text, '-') || '|' || "
    "category_manual::text || '|' || amount::text || '|' || updated_at::text, ',' "
    "ORDER BY id))"
)


def _массив(номера) -> str:
    return "ARRAY[%s]::int[]" % ",".join(str(int(н)) for н in sorted(номера))


def собрать_sql(по_организациям: dict, фиксировать: bool = False) -> str:
    """SQL прохода: {org_id: [номера]} → одна транзакция, UPDATE на организацию.

    По умолчанию ROLLBACK: первый прогон — показать, не менять.
    ⚠️ ОРГАНИЗАЦИЯ В УСЛОВИИ У КАЖДОГО UPDATE, И СЧЁТ В КОНЦЕ ОБЯЗАН СОЙТИСЬ:
    номер из чужой организации или несуществующий иначе выпал бы молча,
    и проход выглядел бы полным."""
    по_организациям = {о: н for о, н in по_организациям.items() if н}
    if not по_организациям:
        raise ValueError("список пуст — проходу нечего делать")
    все = sorted(н for номера in по_организациям.values() for н in номера)
    обновления = []
    for org_id in sorted(по_организациям):
        обновления += [
            "UPDATE receipts SET category_confirm_required = TRUE",
            " WHERE id = ANY(%s) AND org_id = %d AND category_manual = FALSE"
            " AND category_confirm_required = FALSE;"
            % (_массив(по_организациям[org_id]), int(org_id)),
        ]
    условие_счёта = " OR ".join(
        "(id = ANY(%s) AND org_id = %d)" % (_массив(н), int(о))
        for о, н in sorted(по_организациям.items())
    )
    return "\n".join(
        [
            "-- CAT-FOOD ②, разовый проход флага подтверждения.",
            "-- Метка прогона: %s. Чеков в списке: %d. Организаций: %d."
            % (
                datetime.datetime.now().isoformat(timespec="seconds"),
                len(все),
                len(по_организациям),
            ),
            "BEGIN;",
            "CREATE TEMP TABLE cat_food_before ON COMMIT DROP AS"
            " SELECT %s AS h FROM receipts;" % _СУММА,
            *обновления,
            "DO $$ BEGIN",
            "  IF (SELECT h FROM cat_food_before) IS DISTINCT FROM"
            " (SELECT %s FROM receipts) THEN" % _СУММА,
            "    RAISE EXCEPTION 'контрольная сумма разошлась: проход изменил не только флаг';",
            "  END IF;",
            "  IF (SELECT count(*) FROM receipts WHERE (%s) AND category_confirm_required)"
            " <> %d THEN" % (условие_счёта, len(все)),
            "    RAISE EXCEPTION 'флаг стоит не у всех чеков списка: номер чужой или его нет';",
            "  END IF;",
            "END $$;",
            "SELECT org_id, count(*) AS с_флагом FROM receipts WHERE (%s)"
            " AND category_confirm_required GROUP BY org_id ORDER BY org_id;"
            % условие_счёта,
            "COMMIT;" if фиксировать else "ROLLBACK;",
        ]
    )


def main() -> int:
    а = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    а.add_argument("таблица")
    а.add_argument("--org", type=int, default=1)
    а.add_argument("--порог", default=str(ПОРОГ), help="число или NULL")
    а.add_argument("--commit", action="store_true")
    п = а.parse_args()
    порог = None if п.порог.upper() == "NULL" else float(п.порог)
    итог = посчитать(разобрать(open(п.таблица, encoding="utf-8").read()), порог)
    print("-- ТАБЛИЦА: id · вывеска · было → станет · тип · флаг прохода")
    for с in итог:
        if с["человек_уже"]:
            # Решение человека: ни категорию, ни флаг проход не трогает,
            # и выгрузку такой чек не держит (Р5: «требует И не подтверждён»).
            станет, знак, флаг = "(не трогается)", " ", "человек"
        else:
            станет = с["станет"]
            знак = "=" if с["было"] == с["станет"] else "→"
            флаг = "ДА" if с["подтвердить"] else "—"
        print(
            "-- %5d · %-34s · %-26s %s %-26s · %-8s · %s"
            % (
                с["id"],
                с["вывеска"][:34],
                с["было"][:26],
                знак,
                станет[:26],
                с["тип"],
                флаг,
            )
        )
    по_организациям = к_проходу(итог, п.org)
    под_флаг = [н for номера in по_организациям.values() for н in номера]
    ручные = [с for с in итог if с["человек_уже"]]
    по_типу: dict = {}
    for с in итог:
        # по готовому списку прохода, а не повтором правила: правило одно — в к_проходу
        if с["id"] in под_флаг:
            причина = "общепит" if с["тип"] in ("catering", "both") else "порог"
            по_типу[причина] = по_типу.get(причина, 0) + 1
    print(
        "-- ЧЕКОВ %d · под флаг %d (%s) на %s ₽ · ручной выбор, не трогаются: %d"
        % (
            len(итог),
            len(под_флаг),
            ", ".join("%s %d" % пара for пара in sorted(по_типу.items())),
            f"{sum(с['сумма'] for с in итог if с['id'] in под_флаг):,.2f}".replace(
                ",", " "
            ),
            len(ручные),
        )
    )
    for org_id, номера in sorted(по_организациям.items()):
        print("-- организация %s: под флаг %d" % (org_id, len(номера)))
    print(собрать_sql(по_организациям, п.commit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
