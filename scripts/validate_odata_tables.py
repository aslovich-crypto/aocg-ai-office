#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ВАЛИДАЦИЯ МИГРАЦИИ «таблицы обмена с 1С» — BEGIN/ROLLBACK, БЕЗ ЗАПИСИ.

⚠️ ЗАКРЕПЛЯЮЩЕЙ КОМАНДЫ ЗДЕСЬ НЕТ НИ ОДНОЙ — только откат. Транзакция
открывается, DDL прогоняется на ЖИВОЙ схеме, состав замеряется через
`information_schema`, и всё откатывается. База остаётся ровно такой же,
какой была. Требование главы 13а.4, оно не смягчается.

⚠️ DDL ПРОГОНЯЕТСЯ ДВАЖДЫ, И ВТОРОЙ РАЗ ВАЖНЕЕ ПЕРВОГО. Первый проверяет
СИНТАКСИС, второй — ИДЕМПОТЕНТНОСТЬ: `init_db()` выполняется при КАЖДОМ
старте контейнера. Ошибка идемпотентности не видна на пустой базе
и роняет приложение на втором старте — уже на проде.

⚠️ СПИСОК ОДИН — ИНАЧЕ ПРОВЕРЯЛИ БЫ ОДНО, А ПРИМЕНЯЛИ ДРУГОЕ. Те же
строки дословно лежат в `init_db()`; `--sql` печатает текст ИЗ ЭТОГО ЖЕ
списка, а не переписывает его рядом. Совпадение стережёт
`tests/test_migration_odata_tables.py`.

⚠️ С БАСТИОНА ПИТОНОМ ЭТОТ ФАЙЛ НЕ ЗАПУСТИТЬ, И ЭТО ЗАМЕР, А НЕ ДОГАДКА:
репозитория на бастионе нет (`/root/aocg-ai-office` не существует), там
база и psql, но не код. Поэтому ключ `--sql` обязателен.

ЗАПУСК:
    · локально, на живом кластере:  python3 scripts/validate_odata_tables.py
    · на бастионе:                  python3 scripts/validate_odata_tables.py --sql
      печатает SQL целиком, дальше вставкой в psql либо `psql "$DATABASE_URL" -f -`

КОДЫ ВОЗВРАТА: 0 — миграция безопасна · 1 — НЕ ГОДИТСЯ · 2 — откат не сработал.
"""

import asyncio
import os
import sys

import asyncpg

ТАБЛИЦЫ = ("odata_category_map", "odata_exports")

# ⚠️ ДОСЛОВНАЯ КОПИЯ ТОГО, ЧТО ЛЕЖИТ В `init_db()`.
МИГРАЦИЯ = [
    """CREATE TABLE IF NOT EXISTS odata_category_map (
    id            SERIAL PRIMARY KEY,
    org_id        INTEGER NOT NULL REFERENCES organizations(id),
    category_id   INTEGER NOT NULL REFERENCES categories(id),
    account_code  TEXT NOT NULL,
    expense_ref   TEXT,
    expense_name  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS odata_category_map_unique
    ON odata_category_map(org_id, category_id)""",
    """CREATE TABLE IF NOT EXISTS odata_exports (
    id           SERIAL PRIMARY KEY,
    org_id       INTEGER NOT NULL REFERENCES organizations(id),
    report_id    INTEGER NOT NULL REFERENCES reports(id),
    user_id      INTEGER REFERENCES users(id),
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    outcome      TEXT NOT NULL,
    doc_ref      TEXT,
    doc_number   TEXT,
    error_note   TEXT
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS odata_exports_one_success
    ON odata_exports(report_id) WHERE outcome = 'ok'""",
    """CREATE INDEX IF NOT EXISTS idx_odata_exports_org
    ON odata_exports(org_id, started_at DESC)""",
]

# ОБРАТНЫЙ DDL — точка отката. Выполняется РУКАМИ, приложением никогда.
ОБРАТНЫЙ_DDL = [
    "DROP INDEX IF EXISTS idx_odata_exports_org",
    "DROP INDEX IF EXISTS odata_exports_one_success",
    "DROP TABLE IF EXISTS odata_exports",
    "DROP INDEX IF EXISTS odata_category_map_unique",
    "DROP TABLE IF EXISTS odata_category_map",
]

# Что обязано появиться. Список — слепок DDL выше, а не вторая правда:
# тест сверяет его с самим DDL построчно.
ОЖИДАЕМЫЕ_КОЛОНКИ = {
    "odata_category_map": (
        "id",
        "org_id",
        "category_id",
        "account_code",
        "expense_ref",
        "expense_name",
        "created_at",
        "updated_at",
    ),
    "odata_exports": (
        "id",
        "org_id",
        "report_id",
        "user_id",
        "started_at",
        "finished_at",
        "outcome",
        "doc_ref",
        "doc_number",
        "error_note",
    ),
}
ОЖИДАЕМЫЕ_ИНДЕКСЫ = (
    "odata_category_map_unique",
    "odata_exports_one_success",
    "idx_odata_exports_org",
)


def напечатать_sql() -> None:
    """SQL для бастиона: BEGIN, миграция, замер, ROLLBACK. Ни одного COMMIT."""
    print("-- ВАЛИДАЦИЯ МИГРАЦИИ «таблицы обмена с 1С» (1C-21, заход ②)")
    print("-- ⚠️ ЗАКРЕПЛЕНИЯ НЕТ: в конце ROLLBACK, база останется как была.")
    print("BEGIN;")
    for команда in МИГРАЦИЯ:
        print(команда.strip() + ";")
    print("-- повтор целиком: init_db выполняется на КАЖДОМ старте контейнера")
    for команда in МИГРАЦИЯ:
        print(команда.strip() + ";")
    print(
        "SELECT table_name, count(*) AS колонок FROM information_schema.columns\n"
        " WHERE table_name IN ('odata_category_map','odata_exports')\n"
        " GROUP BY table_name ORDER BY table_name;"
    )
    print(
        "SELECT indexname FROM pg_indexes\n"
        " WHERE tablename IN ('odata_category_map','odata_exports') ORDER BY indexname;"
    )
    print("ROLLBACK;")
    print("-- Ожидается: odata_category_map 8 колонок, odata_exports 10;")
    print("-- индексов пять: два первичных ключа плюс три названных.")


async def прогнать(адрес: str) -> int:
    соединение = await asyncpg.connect(адрес)
    try:
        # ⚠️ СНИМАЕМ СОСТОЯНИЕ ДО НАЧАЛА, И ЭТО НЕ ПЕДАНТИЗМ. Откат обязан
        # вернуть базу в то состояние, какое БЫЛО, а не в пустое: на проде
        # `init_db` уже создавал эти таблицы при прошлом старте, и они там
        # есть законно. Первая редакция сравнивала с нулём и кричала «откат
        # не сработал» на совершенно исправной базе — поймано живым тестом
        # `test_рельсы_проходят_и_когда_таблицы_уже_есть`.
        было = {
            з["table_name"]
            for з in await соединение.fetch(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_name = ANY($1::text[])",
                list(ТАБЛИЦЫ),
            )
        }
        if было:
            print("  (таблицы уже есть в базе: %s)" % ", ".join(sorted(было)))
        сделка = соединение.transaction()
        await сделка.start()
        try:
            for заход in (1, 2):
                for команда in МИГРАЦИЯ:
                    await соединение.execute(команда)
                print("  прогон %d: DDL выполнен" % заход)

            for таблица, колонки in ОЖИДАЕМЫЕ_КОЛОНКИ.items():
                есть = {
                    з["column_name"]
                    for з in await соединение.fetch(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = $1",
                        таблица,
                    )
                }
                нет = [к for к in колонки if к not in есть]
                if нет:
                    print("  ✗ %s: не появились колонки %s" % (таблица, ", ".join(нет)))
                    return 1
                print("  ✓ %s: колонок %d" % (таблица, len(есть)))

            индексы = {
                з["indexname"]
                for з in await соединение.fetch(
                    "SELECT indexname FROM pg_indexes WHERE tablename = ANY($1::text[])",
                    list(ТАБЛИЦЫ),
                )
            }
            нет = [и for и in ОЖИДАЕМЫЕ_ИНДЕКСЫ if и not in индексы]
            if нет:
                print("  ✗ не появились индексы: %s" % ", ".join(нет))
                return 1
            print("  ✓ индексы на месте: %s" % ", ".join(ОЖИДАЕМЫЕ_ИНДЕКСЫ))
        finally:
            await сделка.rollback()

        # ⚠️ ПРОВЕРЯЕМ, ЧТО ОТКАТ ВЕРНУЛ БАЗУ В ПРЕЖНЕЕ СОСТОЯНИЕ. Валидация,
        # оставившая за собой НОВУЮ таблицу, — это не валидация, а тихая
        # миграция. Сравниваем со снимком «до», а не с нулём.
        стало = {
            з["table_name"]
            for з in await соединение.fetch(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_name = ANY($1::text[])",
                list(ТАБЛИЦЫ),
            )
        }
        появились = стало - было
        if появились:
            print(
                "  ✗✗ ОТКАТ НЕ СРАБОТАЛ: за проверкой остались %s"
                % ", ".join(sorted(появились))
            )
            return 2
        print("  ✓ откат сработал: база как была")
        return 0
    finally:
        await соединение.close()


def main() -> int:
    if "--sql" in sys.argv:
        напечатать_sql()
        return 0
    адрес = os.getenv("DATABASE_URL", "").strip()
    if not адрес:
        print("DATABASE_URL не задан. Запускать с бастиона либо с ключом --sql.")
        return 1
    print("\nВАЛИДАЦИЯ «таблицы обмена с 1С»: BEGIN → DDL ×2 → замер → ROLLBACK\n")
    return asyncio.run(прогнать(адрес))


if __name__ == "__main__":
    sys.exit(main())
