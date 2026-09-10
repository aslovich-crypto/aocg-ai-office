#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ВАЛИДАЦИЯ МИГРАЦИИ «время чека без ложной зоны» (строка 20) — BEGIN/ROLLBACK.

⚠️ ЗАКРЕПЛЯЮЩЕЙ КОМАНДЫ ЗДЕСЬ НЕТ НИ ОДНОЙ — только откат. Транзакция
открывается, DDL прогоняется на ЖИВОЙ схеме, результат замеряется — и всё
откатывается. База остаётся ровно такой же, какой была.

ЧТО ЧИНИМ. `receipts.datetime` объявлена `TIMESTAMP WITH TIME ZONE`, а ФНС
отдаёт СТЕННОЕ время кассы наивной строкой без зоны («2026-08-11T21:34:00»).
Значение попадает в колонку и получает метку `+00:00`, которой касса
не присылала. Метка ЛОЖНА: в России одиннадцать часовых поясов, и зоны
кассы мы не знаем. Честная форма — `TIMESTAMP WITHOUT TIME ZONE`: значение
то же, снимается только обещание зоны.

⚠️ ГЛАВНОЕ, РАДИ ЧЕГО ЭТОТ ФАЙЛ ВООБЩЕ НУЖЕН — `USING ... AT TIME ZONE`.
Голый `ALTER COLUMN ... TYPE TIMESTAMP` конвертирует ПО ПОЯСУ СЕССИИ.
Замер на живом PostgreSQL 11.09.2026, значение `2026-08-11 21:34:00+00`:
    TZ=UTC             → 2026-08-11 21:34:00   верно
    TZ=Europe/Moscow   → 2026-08-12 00:34:00   СДВИГ НА ТРИ ЧАСА
    USING ... 'UTC'    → 2026-08-11 21:34:00   верно при любом поясе
**ALTER при этом отрабатывает без единой ошибки** — ни исключения, ни
предупреждения. Прод в UTC, локальный кластер в Europe/Moscow (замер T36
06.09.2026), то есть на машине разработчика та же миграция молча сдвинула
бы все чеки и выглядела успехом. Класс: правка верна только при НЕНАЗВАННОМ
условии — а раз условие не названо, его никто и не проверит.

⚠️ СВЕРКА СРАВНИВАЕТ ЗНАЧЕНИЯ ДО И ПОСЛЕ, А НЕ ТОЛЬКО ТИП (требование
владельца 11.09.2026). Тип поменять легко и легко при этом испортить данные;
сдвиг обязан быть НУЛЕВЫМ у КАЖДОГО проверенного чека, и печатается он
поимённо, а не «всё хорошо».

⚠️ СПИСОК ОДИН — иначе проверяли бы одно, а применяли другое. Те же строки
дословно уедут в `init_db()`; совпадение будет стеречь
`tests/test_migration_receipt_datetime.py`.

⚠️ ЭТО ПОЛОВИНА РАБОТЫ, И ВТОРУЮ НЕЛЬЗЯ ДЕЛАТЬ РАНЬШЕ. Во фронте стоит
компенсация `timeZone: "UTC"` (`src/lib/format.js:44`), которая нужна ровно
пока метка лжёт. Снять её ДО смены типа — экран уедет на три часа.
Порядок неразделим: сначала тип, потом компенсация, одной работой.

ЗАПУСК (с бастиона, где есть DATABASE_URL):
    python3 scripts/validate_receipt_datetime.py
"""

import asyncio
import os
import sys

import asyncpg

ТАБЛИЦА = "receipts"
КОЛОНКА = "datetime"

# ⚠️ `USING ... AT TIME ZONE 'UTC'` — НЕ УКРАШЕНИЕ, А СУТЬ ПРАВКИ.
# Без него результат зависит от пояса сессии (см. замер в шапке).
# `AT TIME ZONE 'UTC'` у значения TIMESTAMPTZ даёт стенное время В UTC,
# то есть ровно то, что касса и напечатала.
МИГРАЦИЯ = [
    f"ALTER TABLE {ТАБЛИЦА} ALTER COLUMN {КОЛОНКА} "
    f"TYPE TIMESTAMP WITHOUT TIME ZONE USING {КОЛОНКА} AT TIME ZONE 'UTC'",
]

# Сколько чеков сверяем поимённо. Не «пару штук для вида»: берём и самые
# свежие, и самые старые — если сдвиг зависит от даты (переход на летнее
# время в чужой зоне), он вылезет именно на краях.
ОБРАЗЦОВ = 10


async def тип(conn) -> str:
    return await conn.fetchval(
        """SELECT data_type FROM information_schema.columns
           WHERE table_name=$1 AND column_name=$2""",
        ТАБЛИЦА,
        КОЛОНКА,
    )


async def образцы(conn) -> dict:
    """id → стенное время ТЕКСТОМ. Текст, а не datetime: сравниваем то,
    что человек прочтёт, а не то, как драйвер это представил."""
    строки = await conn.fetch(
        f"""(SELECT id, to_char({КОЛОНКА} AT TIME ZONE 'UTC',
                                'YYYY-MM-DD HH24:MI:SS') AS т
             FROM {ТАБЛИЦА} WHERE {КОЛОНКА} IS NOT NULL
             ORDER BY id DESC LIMIT {ОБРАЗЦОВ})
            UNION
            (SELECT id, to_char({КОЛОНКА} AT TIME ZONE 'UTC',
                                'YYYY-MM-DD HH24:MI:SS') AS т
             FROM {ТАБЛИЦА} WHERE {КОЛОНКА} IS NOT NULL
             ORDER BY id ASC LIMIT {ОБРАЗЦОВ})"""
    )
    return {с["id"]: с["т"] for с in строки}


async def образцы_после(conn) -> dict:
    """То же после смены типа: колонка уже без зоны, приводить нечего."""
    строки = await conn.fetch(
        f"""(SELECT id, to_char({КОЛОНКА}, 'YYYY-MM-DD HH24:MI:SS') AS т
             FROM {ТАБЛИЦА} WHERE {КОЛОНКА} IS NOT NULL
             ORDER BY id DESC LIMIT {ОБРАЗЦОВ})
            UNION
            (SELECT id, to_char({КОЛОНКА}, 'YYYY-MM-DD HH24:MI:SS') AS т
             FROM {ТАБЛИЦА} WHERE {КОЛОНКА} IS NOT NULL
             ORDER BY id ASC LIMIT {ОБРАЗЦОВ})"""
    )
    return {с["id"]: с["т"] for с in строки}


async def main() -> int:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL не задан — валидировать нечего")
        return 1
    conn = await asyncpg.connect(dsn)
    try:
        пояс = await conn.fetchval("SHOW TimeZone")
        всего = await conn.fetchval(f"SELECT count(*) FROM {ТАБЛИЦА}")
        со_временем = await conn.fetchval(
            f"SELECT count(*) FROM {ТАБЛИЦА} WHERE {КОЛОНКА} IS NOT NULL"
        )
        было_тип = await тип(conn)
        было = await образцы(conn)
        print(f"ПОЯС СЕССИИ: {пояс}")
        print(f"ЧТО БЫЛО: чеков {всего}, со временем {со_временем}, тип {было_тип}")
        print(f"          образцов для сверки: {len(было)}")

        tr = conn.transaction()
        await tr.start()
        try:
            for ddl in МИГРАЦИЯ:
                await conn.execute(ddl)
            стало_тип = await тип(conn)
            стало = await образцы_после(conn)
            print(f"ПОСЛЕ ALTER: тип {стало_тип}")

            # ⚠️ СВЕРКА ПОИМЁННАЯ. «Расхождений нет» без списка проверенных
            # неотличимо от «мы ничего не проверяли».
            сдвинулись = {
                ид: (было[ид], стало.get(ид))
                for ид in было
                if стало.get(ид) != было[ид]
            }
            print(f"СВЕРКА ЗНАЧЕНИЙ: сверено {len(было)}, сдвинулось {len(сдвинулись)}")
            for ид, (б, с) in sorted(сдвинулись.items()):
                print(f"  ✗ чек {ид}: было {б} → стало {с}")
            if not сдвинулись:
                примеры = sorted(было.items())[:3]
                for ид, т in примеры:
                    print(f"  ✓ чек {ид}: {т} — без изменений")
            годно = стало_тип == "timestamp without time zone" and not сдвинулись
        finally:
            await tr.rollback()
            print("ROLLBACK выполнен — база не изменена")

        снова = await тип(conn)
        print(f"ПРОВЕРКА ОТКАТА: тип снова {снова}")
        if снова != было_тип:
            print("⚠️ ОТКАТ НЕ СРАБОТАЛ — схема изменилась, разбираться немедленно")
            return 2
        print("ИТОГ:", "миграция безопасна" if годно else "⚠️ МИГРАЦИЯ НЕ ГОДИТСЯ")
        return 0 if годно else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
