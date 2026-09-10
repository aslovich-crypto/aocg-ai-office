# -*- coding: utf-8 -*-
"""Миграция «время чека» НА ДАННЫХ: сдвиг обязан быть нулевым (строка 20).

⚠️ ЗАЧЕМ ЗАВЕДЁН, ЗАМЕРОМ 11.09.2026. Мутация М1 («`USING` убран») была
поймана ТОЛЬКО структурным сторожем — живой контур смолчал. Причина
не в лени прибора, а в порядке событий: DDL миграции выполняется в
`init_db()`, когда таблица ПУСТА, и сдвигать там нечего. То есть весь
существующий живой контур не мог бы поймать порчу данных НИКОГДА.
Единственной проверкой на данных оставался прогон владельца на бастионе —
разовый, руками, и повторить его перед следующей правкой было бы нечем.

⚠️ ЗДЕСЬ ЗАМЕР ПРЕВРАЩЁН В ПРИБОР (наблюдение 14: замер, повторённый
в третий раз, обязан стать прибором — бастион 10.09, psql 11.09, и вот).
Тест воспроизводит бастионную валидацию: кладёт чек с известным стенным
временем, возвращает колонке ложную зону, прогоняет ТУ ЖЕ строку DDL
и сверяет значение до и после.

⚠️ ГЛАВНОЕ — ПОЯС КЛАСТЕРА. Проверка имеет смысл ровно тогда, когда пояс
сессии НЕ UTC: голый ALTER конвертирует по поясу сессии, и при UTC он
случайно верен. Локальный кластер поднимается в поясе системы
(Europe/Moscow, замер T36), но в CI это UTC — поэтому пояс задаётся здесь
явно, и тест честно говорит, что именно он проверил.

⚠️ СТРОКА DDL БЕРЁТСЯ ИЗ РЕЛЬСОВ, А НЕ ПЕРЕПИСЫВАЕТСЯ РЯДОМ. Иначе тест
стерёг бы собственную копию, а в `init_db()` уехало бы что угодно.
"""

import importlib.util
import pathlib

import pytest

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
РЕЛЬСЫ = КОРЕНЬ / "scripts" / "validate_receipt_datetime.py"

pytestmark = pytest.mark.asyncio


def _миграция() -> list[str]:
    спец = importlib.util.spec_from_file_location("рельсы_время_чека", РЕЛЬСЫ)
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)
    return list(модуль.МИГРАЦИЯ)


async def test_смена_типа_не_двигает_время_в_чужом_поясе(db):
    ddl = _миграция()
    assert ddl, "рельсы не отдали ни одной строки миграции"

    async with db.pool.acquire() as conn:
        # ⚠️ ЧУЖОЙ ПОЯС — УСЛОВИЕ ПРОВЕРКИ, А НЕ ОБСТАНОВКА. При UTC голый
        # ALTER верен случайно, и тест зеленел бы на сломанном DDL.
        await conn.execute("SET TimeZone = 'Europe/Moscow'")
        пояс = await conn.fetchval("SHOW TimeZone")
        assert пояс == "Europe/Moscow", f"пояс не встал: {пояс}"

        # Возвращаем колонке ложную зону — состояние прода ДО миграции.
        await conn.execute(
            "ALTER TABLE receipts ALTER COLUMN datetime "
            "TYPE TIMESTAMP WITH TIME ZONE USING datetime AT TIME ZONE 'UTC'"
        )
        # Стенное время кассы, как его отдаёт ФНС: строка без зоны.
        # Два края суток намеренно: сдвиг на полуночном чеке меняет ДАТУ.
        # ⚠️ Литералом в SQL, а не параметром: asyncpg требует под timestamptz
        # объект datetime, то есть заставил бы ПИТОН решить вопрос про зону —
        # ровно тот, который мы и проверяем. Пусть решает PostgreSQL.
        await conn.execute(
            "INSERT INTO receipts (id, org, date, datetime) VALUES "
            "(1, 'Проба', '2026-08-11', '2026-08-11 21:34:00+00'), "
            "(2, 'Проба', '2026-08-12', '2026-08-12 00:30:00+00')"
        )

        async def стенное() -> dict:
            """id → стенное время ТЕКСТОМ, как его прочтёт человек."""
            выражение = (
                "datetime AT TIME ZONE 'UTC'"
                if (
                    await conn.fetchval(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_name='receipts' AND column_name='datetime'"
                    )
                )
                == "timestamp with time zone"
                else "datetime"
            )
            return {
                с["id"]: с["т"]
                for с in await conn.fetch(
                    f"SELECT id, to_char({выражение}, "
                    "'YYYY-MM-DD HH24:MI:SS') AS т FROM receipts ORDER BY id"
                )
            }

        было = await стенное()
        assert было == {
            1: "2026-08-11 21:34:00",
            2: "2026-08-12 00:30:00",
        }, f"подготовка неверна: {было}"

        for строка in ddl:
            await conn.execute(строка)

        тип = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='receipts' AND column_name='datetime'"
        )
        assert тип == "timestamp without time zone", тип

        стало = await стенное()
        сдвиг = {и: (было[и], стало.get(и)) for и in было if стало.get(и) != было[и]}
        assert not сдвиг, (
            "миграция СДВИНУЛА время чеков при поясе Europe/Moscow — "
            f"это порча данных, а не смена типа: {сдвиг}"
        )
