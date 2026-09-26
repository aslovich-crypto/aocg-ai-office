#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ВАЛИДАЦИЯ МИГРАЦИИ FIN-01 «справочник статей CoA» — BEGIN/ROLLBACK, БЕЗ ЗАПИСИ.

⚠️ ЗАКРЕПЛЯЮЩЕЙ КОМАНДЫ ЗДЕСЬ НЕТ НИ ОДНОЙ — только откат. Транзакция
открывается, DDL прогоняется на ЖИВОЙ схеме, состав замеряется через
`information_schema` и `pg_indexes`, и всё откатывается. База остаётся ровно
такой же, какой была. Требование главы 13а.4, оно не смягчается.

⚠️ DDL ПРОГОНЯЕТСЯ ДВАЖДЫ, И ВТОРОЙ РАЗ ВАЖНЕЕ ПЕРВОГО. Первый проверяет
СИНТАКСИС, второй — ИДЕМПОТЕНТНОСТЬ: `init_db()` выполняется при КАЖДОМ старте
контейнера. Ошибка идемпотентности не видна на пустой базе и роняет приложение
на втором старте — уже на проде.

⚠️ ТЕКСТ МИГРАЦИИ НЕ ПЕРЕПИСАН РЯДОМ, А ИМПОРТИРОВАН из `app.finance.schema`.
У образца `validate_odata_tables.py` список лежит копией, и его совпадение
с `init_db()` приходится стеречь отдельным тестом. Здесь копии нет вовсе —
разойтись нечему, поэтому и сторожа на совпадение нет.

⚠️ С БАСТИОНА ПИТОНОМ ЭТОТ ФАЙЛ НЕ ЗАПУСТИТЬ: репозитория там нет, там база
и psql, но не код. Поэтому ключ `--sql` обязателен — он печатает тот же текст
для вставки в psql. И отдельно: из оболочки агента прод недоступен в принципе
(ключа к бастиону нет), значит прогон против боевой базы делает ВЛАДЕЛЕЦ,
а агент может проверить только локальный кластер.

ЗАПУСК:
    · локально на живом кластере:  DATABASE_URL=postgresql://... python3 scripts/validate_fin_coa_tables.py
    · текст для бастиона:          python3 scripts/validate_fin_coa_tables.py --sql
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.finance.coa_seed import СТАТЬИ  # noqa: E402
from app.finance.schema import DDL  # noqa: E402

ТАБЛИЦА = "fin_coa_articles"
ОЖИДАЕМЫЕ_КОЛОНКИ = (
    "id",
    "org_id",
    "code",
    "name_ru",
    "name_en",
    "section",
    "kind",
    "is_cash",
    "is_group",
    "is_reserve",
    "is_active",
    "note",
    "position",
    "coa_version",
    "created_at",
)
ОЖИДАЕМЫЕ_ИНДЕКСЫ = ("ux_fin_coa_org_code", "idx_fin_coa_org")


def печать_sql() -> int:
    """Текст для бастиона: BEGIN, миграция дважды, замер, ROLLBACK. Без COMMIT."""
    print(
        "-- ВАЛИДАЦИЯ FIN-01 · справочник статей CoA (%d статей в сиде)" % len(СТАТЬИ)
    )
    print("-- ⚠️ ЗАКРЕПЛЕНИЯ НЕТ: в конце ROLLBACK, база останется как была.")
    print("BEGIN;")
    for заход in (1, 2):
        print("-- прогон %d из 2 (второй проверяет идемпотентность)" % заход)
        for команда in DDL:
            print(команда.strip() + ";")
    print(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = '%s' ORDER BY ordinal_position;" % ТАБЛИЦА
    )
    print("SELECT indexname FROM pg_indexes WHERE tablename = '%s';" % ТАБЛИЦА)
    print("SELECT count(*) AS статей_после_отката_будет_ноль FROM %s;" % ТАБЛИЦА)
    print("ROLLBACK;")
    print("-- откат выполнен: таблицы нет, как и не было")
    return 0


async def прогон(адрес: str) -> int:
    import asyncpg

    соединение = await asyncpg.connect(адрес)
    try:
        сделка = соединение.transaction()
        await сделка.start()
        try:
            for заход in (1, 2):
                for команда in DDL:
                    await соединение.execute(команда)
                print("  прогон %d: DDL выполнен" % заход)

            есть = {
                з["column_name"]
                for з in await соединение.fetch(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = $1",
                    ТАБЛИЦА,
                )
            }
            нет = [к for к in ОЖИДАЕМЫЕ_КОЛОНКИ if к not in есть]
            if нет:
                print("  ✗ не появились колонки: %s" % ", ".join(нет))
                return 1
            print(
                "  ✓ колонок: %d (все %d ожидаемых на месте)"
                % (len(есть), len(ОЖИДАЕМЫЕ_КОЛОНКИ))
            )

            индексы = {
                з["indexname"]
                for з in await соединение.fetch(
                    "SELECT indexname FROM pg_indexes WHERE tablename = $1", ТАБЛИЦА
                )
            }
            нет = [и for и in ОЖИДАЕМЫЕ_ИНДЕКСЫ if и not in индексы]
            if нет:
                print("  ✗ не появились индексы: %s" % ", ".join(нет))
                return 1
            print("  ✓ индексы на месте: %s" % ", ".join(ОЖИДАЕМЫЕ_ИНДЕКСЫ))

            строк = await соединение.fetchval("SELECT count(*) FROM %s" % ТАБЛИЦА)
            print(
                "  ✓ контрольный SELECT: строк в таблице %s (засев здесь не гонялся)"
                % строк
            )
            return 0
        finally:
            await сделка.rollback()
            print("  ✓ ROLLBACK выполнен — база осталась как была")
    finally:
        await соединение.close()


def main() -> int:
    if "--sql" in sys.argv:
        return печать_sql()
    адрес = os.getenv("DATABASE_URL", "").strip()
    if not адрес:
        print("DATABASE_URL не задан. Запускать на живом кластере либо с ключом --sql.")
        return 2
    print("ВАЛИДАЦИЯ FIN-01 · %s" % ТАБЛИЦА)
    return asyncio.run(прогон(адрес))


if __name__ == "__main__":
    sys.exit(main())
