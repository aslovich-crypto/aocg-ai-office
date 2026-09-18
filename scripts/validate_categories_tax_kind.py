#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""СВЕРКА ВИДОВ РАСХОДА В БАЗЕ СО СЛОВАРЁМ + ПРАВКА ДВУХ СТРОК (Cat).

⚠️ ЗАЧЕМ ЭТОТ ПРИБОР ВООБЩЕ. Механизм словаря сторожит СОГЛАСИЕ КОПИЙ
с источником: `dict_stamp`, `gen_dictionaries --check`, `check-dictionaries`.
Ни один из них не смотрит в БАЗУ. А `seed_default_categories` (файл
`app/categories_seed.py`, строки 30–34) для уже засеянной организации —
no-op: он НИЧЕГО НЕ ОБНОВЛЯЕТ. Значит правка словаря на прод сама не
доезжает никогда, и узнать об этом можно только счётом по базе.

⚠️ ДВА РЕЖИМА, И ОНИ НАЗЫВАЮТСЯ ВСЛУХ. `--sql` печатает сухой прогон
с ROLLBACK: видно, что лежит сейчас, сколько строк изменится и сколько
чеков сменит налоговый смысл. `--sql-apply` печатает тот же текст
с COMMIT. Перепутать нельзя: закрепляющий вариант кричит о себе.

⚠️ ПРАВКА ОГРАНИЧЕНА ОРГАНИЗАЦИЕЙ. У каждой организации свой набор
категорий, и «затронуто ровно две строки» верно только для одной.
Первый запрос показывает, скольких организаций это касается вообще.

⚠️ ЧТО МЕНЯЕТСЯ И ПОЧЕМУ (решение владельца 17.09.2026):
  «Кейтеринг для встреч» → Представительские расходы (п. 2 ст. 264 НК,
      норматив 4 % ФОТ; норматив считает 1С, мы только помечаем вид);
  «Подарки и поощрения» → Не учитываемые в целях налогообложения
      (п. 16 ст. 270 НК).
Категории 6, 7, 8 НЕ трогаются — отдельное решение владельца.

ЗАПУСК:
    · сухой прогон текстом:      python3 scripts/validate_categories_tax_kind.py --sql
    · закрепляющий текст:        python3 scripts/validate_categories_tax_kind.py --sql-apply
    · сверка на живой базе:      python3 scripts/validate_categories_tax_kind.py
    · сверка + правка:           python3 scripts/validate_categories_tax_kind.py --apply

КОДЫ ВОЗВРАТА: 0 — база сошлась со словарём · 1 — есть расхождения.
"""

import asyncio
import os
import sys

# ⚠️ Драйвер базы импортируется только в функции подключения: печать SQL
# нужна владельцу на машине, где `asyncpg` нет, — как в рельсах миграции.

ОРГАНИЗАЦИЯ = 1

# Что правим на проде. Берётся НЕ из словаря целиком, а списком: правка
# двух строк и сверка всех сорока восьми — разные действия, и смешивать
# их значило бы чинить молча то, что решено не трогать (6, 7, 8).
ПРАВКА = (
    ("Кейтеринг для встреч", "Представительские расходы"),
    ("Подарки и поощрения", "Не учитываемые в целях налогообложения"),
)


def _словарь() -> list:
    """[(имя категории, вид расхода)] из единого источника."""
    корень = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if корень not in sys.path:
        sys.path.insert(0, корень)
    from app.dictionaries import DEFAULT_CATEGORIES

    return [(имя, вид) for _г, статьи in DEFAULT_CATEGORIES for имя, вид in статьи]


def напечатать_sql(закрепить: bool) -> None:
    пары = ",\n           ".join("('%s', '%s')" % (имя, вид) for имя, вид in ПРАВКА)
    print(
        "-- ВИДЫ РАСХОДА: СВЕРКА И ПРАВКА ДВУХ СТРОК (Cat), организация %d"
        % ОРГАНИЗАЦИЯ
    )
    if закрепить:
        print("-- ⚠️⚠️ ЭТО ЗАПИСЬ: в конце COMMIT. Сначала прогоните сухой вариант.")
    else:
        print("-- ⚠️ СУХОЙ ПРОГОН: в конце ROLLBACK, база останется как была.")
    print("BEGIN;")
    print(
        "-- ① Что лежит сейчас и у скольких организаций.\n"
        "SELECT c.org_id, c.name, c.tax_kind FROM categories c\n"
        " WHERE c.name IN (%s)\n"
        " ORDER BY c.org_id, c.name;" % ", ".join("'%s'" % имя for имя, _ in ПРАВКА)
    )
    print(
        "-- ② Чеки, которые сменят налоговый смысл. Замер ДО правки.\n"
        "SELECT c.name, count(r.id) AS чеков, coalesce(sum(r.amount), 0) AS сумма\n"
        "  FROM categories c LEFT JOIN receipts r ON r.category_id = c.id\n"
        " WHERE c.org_id = %d AND c.name IN (%s)\n"
        " GROUP BY c.name ORDER BY c.name;"
        % (ОРГАНИЗАЦИЯ, ", ".join("'%s'" % имя for имя, _ in ПРАВКА))
    )
    print(
        "-- ③ Правка. Только организация %d, только если значение ДРУГОЕ.\n"
        "--    ОЖИДАЕТСЯ РОВНО %d.\n"
        "WITH правка(name, tax_kind) AS (\n"
        "    VALUES %s\n"
        "), сделано AS (\n"
        "    UPDATE categories c SET tax_kind = п.tax_kind\n"
        "      FROM правка п\n"
        "     WHERE c.org_id = %d AND c.name = п.name AND c.tax_kind <> п.tax_kind\n"
        "    RETURNING c.id\n"
        ")\n"
        "SELECT count(*) AS затронуто FROM сделано;"
        % (ОРГАНИЗАЦИЯ, len(ПРАВКА), пары, ОРГАНИЗАЦИЯ)
    )
    print(
        "-- ④ Что стало. Обе строки обязаны показать новые значения.\n"
        "SELECT c.name, c.tax_kind FROM categories c\n"
        " WHERE c.org_id = %d AND c.name IN (%s) ORDER BY c.name;"
        % (ОРГАНИЗАЦИЯ, ", ".join("'%s'" % имя for имя, _ in ПРАВКА))
    )
    print("COMMIT;" if закрепить else "ROLLBACK;")
    for имя, вид in ПРАВКА:
        print("-- правим: %s → %s" % (имя, вид))


async def прогнать(адрес: str, закрепить: bool) -> int:
    import asyncpg

    словарь = dict(_словарь())
    соединение = await asyncpg.connect(адрес)
    try:
        сделка = соединение.transaction()
        await сделка.start()
        закреплено = False
        try:
            if закрепить:
                затронуто = await соединение.fetch(
                    "UPDATE categories c SET tax_kind = п.tax_kind"
                    "  FROM (SELECT * FROM unnest($1::text[], $2::text[])"
                    "          AS т(name, tax_kind)) п"
                    " WHERE c.org_id = $3 AND c.name = п.name"
                    "   AND c.tax_kind <> п.tax_kind RETURNING c.name",
                    [имя for имя, _ in ПРАВКА],
                    [вид for _, вид in ПРАВКА],
                    ОРГАНИЗАЦИЯ,
                )
                print("  правка затронула строк: %d" % len(затронуто))
                for з in затронуто:
                    print("    %s" % з["name"])

            # ⚠️ СВЕРЯЕМ ВСЕ СОРОК ВОСЕМЬ, А НЕ ТОЛЬКО ДВЕ ПРАВЛЕНЫЕ: прибор
            # отвечает на вопрос «база равна словарю», а не «моя правка легла».
            строки = await соединение.fetch(
                "SELECT name, tax_kind FROM categories WHERE org_id=$1", ОРГАНИЗАЦИЯ
            )
            расхождения = [
                (з["name"], з["tax_kind"], словарь[з["name"]])
                for з in строки
                if з["name"] in словарь and з["tax_kind"] != словарь[з["name"]]
            ]
            нет_в_базе = sorted(set(словарь) - {з["name"] for з in строки})
            print("  категорий у организации %d: %d" % (ОРГАНИЗАЦИЯ, len(строки)))
            for имя, в_базе, в_словаре in расхождения:
                print(
                    "    ✗ %s: в базе «%s», в словаре «%s»" % (имя, в_базе, в_словаре)
                )
            for имя in нет_в_базе:
                print("    ✗ %s: есть в словаре, нет в базе" % имя)
            if расхождения or нет_в_базе:
                print("  ✗ БАЗА РАЗОШЛАСЬ СО СЛОВАРЁМ")
                return 1
            print("  ✓ база совпадает со словарём по всем видам расхода")
            if закрепить:
                await сделка.commit()
                закреплено = True
                print("  ✓ ЗАКРЕПЛЕНО")
            else:
                print("  ✓ сухой прогон: ничего не записано")
        finally:
            if not закреплено:
                await сделка.rollback()
        return 0
    finally:
        await соединение.close()


def main() -> int:
    if "--sql" in sys.argv or "--sql-apply" in sys.argv:
        напечатать_sql("--sql-apply" in sys.argv)
        return 0
    адрес = os.getenv("DATABASE_URL", "").strip()
    if not адрес:
        print("DATABASE_URL не задан. Запускать с бастиона либо с ключом --sql.")
        return 1
    закрепить = "--apply" in sys.argv
    print(
        "\nСВЕРКА ВИДОВ РАСХОДА: %s\n"
        % ("С ПРАВКОЙ (--apply)" if закрепить else "только сверка, запись выключена")
    )
    return asyncio.run(прогнать(адрес, закрепить))


if __name__ == "__main__":
    sys.exit(main())
