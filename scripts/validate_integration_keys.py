#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ВАЛИДАЦИЯ МИГРАЦИИ «ключи интеграции» — BEGIN/ROLLBACK, БЕЗ ЗАПИСИ.

⚠️ ЗАКРЕПЛЯЮЩЕЙ КОМАНДЫ ЗДЕСЬ НЕТ НИ ОДНОЙ — только откат. Транзакция
открывается, DDL прогоняется на ЖИВОЙ схеме, результат замеряется через
`information_schema` — и всё откатывается. База остаётся ровно такой же,
какой была. Это требование главы 13а.4, и оно не смягчается.

⚠️ DDL ПРОГОНЯЕТСЯ ДВАЖДЫ, И ВТОРОЙ РАЗ ВАЖНЕЕ ПЕРВОГО. Первый прогон
проверяет СИНТАКСИС, второй — ИДЕМПОТЕНТНОСТЬ: `init_db()` выполняется
при КАЖДОМ старте контейнера, то есть этот DDL пойдёт не один раз, а
столько, сколько будет перезапусков. Ошибка идемпотентности не видна на
пустой базе и роняет приложение на втором старте — уже на проде.

⚠️ СПИСОК ОДИН — ИНАЧЕ ПРОВЕРЯЛИ БЫ ОДНО, А ПРИМЕНЯЛИ ДРУГОЕ. Те же
строки дословно лежат в `init_db()`; `--sql` собирает текст ИЗ ЭТОГО ЖЕ
списка, а не переписывает его рядом.

⚠️ С БАСТИОНА ПИТОНОМ ЭТОТ ФАЙЛ НЕ ЗАПУСТИТЬ, И ЭТО ЗАМЕР, А НЕ ДОГАДКА.
Репозитория на бастионе НЕТ (`/root/aocg-ai-office` не существует), там
база и psql, но не код — выяснено 10.09.2026, когда владелец гонял прошлую
валидацию SQL-файлом. Поэтому ключ `--sql` обязателен: он печатает текст,
годный для вставки в psql.

ЗАПУСК:
    · локально, на живом кластере:  python3 scripts/validate_integration_keys.py
    · на бастионе:                  python3 scripts/validate_integration_keys.py --sql
      печатает SQL целиком, дальше вставкой в psql либо `psql "$DATABASE_URL" -f -`

КОДЫ ВОЗВРАТА: 0 — миграция безопасна · 1 — НЕ ГОДИТСЯ · 2 — откат не сработал.
"""

import asyncio
import os
import sys

import asyncpg

ТАБЛИЦА = "integration_keys"

# ⚠️ ДОСЛОВНАЯ КОПИЯ ТОГО, ЧТО ЛЕЖИТ В `init_db()`. Разъехаться им нечем:
# `--sql` печатает этот же список, и проверяется ровно то, что применится.
МИГРАЦИЯ = [
    """CREATE TABLE IF NOT EXISTS integration_keys (
    id           SERIAL PRIMARY KEY,
    org_id       INTEGER NOT NULL REFERENCES organizations(id),
    prefix       VARCHAR(12) NOT NULL,
    secret_hash  TEXT NOT NULL,
    name         TEXT NOT NULL,
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    revoked_by   INTEGER REFERENCES users(id),
    last_used_at TIMESTAMPTZ,
    last_used_ip VARCHAR(45),
    use_count    BIGINT NOT NULL DEFAULT 0
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS integration_keys_prefix_unique
    ON integration_keys(prefix)""",
    """CREATE INDEX IF NOT EXISTS idx_integration_keys_org
    ON integration_keys(org_id) WHERE revoked_at IS NULL""",
]

# Точка отката. Выполняется РУКАМИ и только если таблицу решат убрать;
# сам этот скрипт ничего не закрепляет и откатывать ему нечего.
ОБРАТНЫЙ_DDL = [
    "DROP INDEX IF EXISTS idx_integration_keys_org",
    "DROP INDEX IF EXISTS integration_keys_prefix_unique",
    "DROP TABLE IF EXISTS integration_keys",
]

# Чего ждём от схемы ПОСЛЕ прогона: имя колонки → тип из information_schema.
ОЖИДАЕМЫЕ_КОЛОНКИ = {
    "id": "integer",
    "org_id": "integer",
    "prefix": "character varying",
    "secret_hash": "text",
    "name": "text",
    "created_by": "integer",
    "created_at": "timestamp with time zone",
    "expires_at": "timestamp with time zone",
    "revoked_at": "timestamp with time zone",
    "revoked_by": "integer",
    "last_used_at": "timestamp with time zone",
    "last_used_ip": "character varying",
    "use_count": "bigint",
}

ОЖИДАЕМЫЕ_ИНДЕКСЫ = {
    "integration_keys_prefix_unique",
    "idx_integration_keys_org",
}


async def колонки(conn) -> dict:
    строки = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=$1",
        ТАБЛИЦА,
    )
    return {с["column_name"]: с["data_type"] for с in строки}


async def индексы(conn) -> set:
    строки = await conn.fetch(
        "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename=$1",
        ТАБЛИЦА,
    )
    return {с["indexname"] for с in строки}


async def есть_таблица(conn) -> bool:
    return await conn.fetchval(
        "SELECT to_regclass($1) IS NOT NULL", "public." + ТАБЛИЦА
    )


async def main() -> int:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL не задан — валидировать нечего")
        return 1
    conn = await asyncpg.connect(dsn)
    try:
        было = await есть_таблица(conn)
        print(
            "ЧТО БЫЛО: таблица %s %s"
            % (ТАБЛИЦА, "СУЩЕСТВУЕТ" if было else "отсутствует")
        )
        if было:
            # ⚠️ НЕ «ОК» И НЕ «ПЛОХО», А ПОВОД ОСТАНОВИТЬСЯ: `IF NOT EXISTS`
            # МОЛЧИТ при столкновении имён, и чужая таблица с тем же именем
            # прошла бы проверку как своя.
            print("⚠️ ТАБЛИЦА УЖЕ ЕСТЬ. Это либо повторный прогон, либо ЧУЖАЯ")
            print("   таблица с тем же именем. Сверьте состав колонок глазами.")

        годно = False
        tr = conn.transaction()
        await tr.start()
        try:
            for ddl in МИГРАЦИЯ:
                await conn.execute(ddl)
            первый_раз = await колонки(conn)

            # ⚠️ ВТОРОЙ ПРОГОН — ПРОВЕРКА ИДЕМПОТЕНТНОСТИ. init_db крутится
            # на КАЖДОМ старте контейнера; DDL, падающий со второго раза,
            # роняет приложение уже на проде, а на пустой базе он зелёный.
            for ddl in МИГРАЦИЯ:
                await conn.execute(ddl)
            второй_раз = await колонки(conn)
            видимые_индексы = await индексы(conn)

            нет_колонок = sorted(set(ОЖИДАЕМЫЕ_КОЛОНКИ) - set(второй_раз))
            не_тот_тип = sorted(
                "%s: ждали %s, получили %s" % (к, т, второй_раз[к])
                for к, т in ОЖИДАЕМЫЕ_КОЛОНКИ.items()
                if к in второй_раз and второй_раз[к] != т
            )
            нет_индексов = sorted(ОЖИДАЕМЫЕ_ИНДЕКСЫ - видимые_индексы)

            print("ПОСЛЕ ПЕРВОГО ПРОГОНА : колонок %d" % len(первый_раз))
            print(
                "ПОСЛЕ ВТОРОГО ПРОГОНА : колонок %d, индексов %d"
                % (len(второй_раз), len(видимые_индексы))
            )
            print(
                "ИДЕМПОТЕНТНОСТЬ       : %s"
                % (
                    "состав не изменился"
                    if первый_раз == второй_раз
                    else "⚠️ СОСТАВ ИЗМЕНИЛСЯ ПОСЛЕ ВТОРОГО ПРОГОНА"
                )
            )
            # ⚠️ ПОИМЁННО, А НЕ ЧИСЛОМ. «Расхождений нет» без списка
            # проверенного неотличимо от «мы ничего не проверяли».
            for к in нет_колонок:
                print("  ✗ колонки нет: %s" % к)
            for с in не_тот_тип:
                print("  ✗ тип не тот: %s" % с)
            for и in нет_индексов:
                print("  ✗ индекса нет: %s" % и)
            if not (нет_колонок or не_тот_тип or нет_индексов):
                for к in sorted(ОЖИДАЕМЫЕ_КОЛОНКИ):
                    print("  ✓ %-13s %s" % (к, второй_раз[к]))
                for и in sorted(ОЖИДАЕМЫЕ_ИНДЕКСЫ):
                    print("  ✓ индекс %s" % и)

            годно = (
                первый_раз == второй_раз
                and not нет_колонок
                and not не_тот_тип
                and not нет_индексов
            )
        finally:
            # ⚠️ В `finally`, А НЕ ПОСЛЕ ПРОВЕРОК: упади любая из них —
            # транзакция обязана откатиться всё равно.
            await tr.rollback()
            print("ROLLBACK выполнен — база не изменена")

        # ⚠️ КОНТРОЛЬНЫЙ SELECT ПОСЛЕ ОТКАТА. Без него «откат выполнен» —
        # это утверждение скрипта о себе, а не замер состояния базы.
        стало = await есть_таблица(conn)
        print(
            "ПРОВЕРКА ОТКАТА: таблица %s %s"
            % (ТАБЛИЦА, "существует" if стало else "отсутствует")
        )
        if стало != было:
            print("⚠️ ОТКАТ НЕ СРАБОТАЛ — схема изменилась, разбираться немедленно")
            return 2
        print("ИТОГ:", "миграция безопасна" if годно else "⚠️ МИГРАЦИЯ НЕ ГОДИТСЯ")
        return 0 if годно else 1
    finally:
        await conn.close()


def SQL_ТЕКСТОМ() -> str:
    """Готовый текст для вставки в psql — СОБИРАЕТСЯ ИЗ СПИСКА `МИГРАЦИЯ`.

    ⚠️ Не переписывается рядом намеренно: два текста одной миграции
    разъезжаются молча, и проверенным окажется одно, а применённым другое.
    """
    ddl = ";\n\n".join(МИГРАЦИЯ)
    откат = ";\n".join(ОБРАТНЫЙ_DDL)
    return (
        "-- ВАЛИДАЦИЯ МИГРАЦИИ «ключи интеграции», BEGIN/ROLLBACK, БЕЗ ЗАПИСИ.\n"
        "-- Ни одной закрепляющей команды здесь нет: транзакция откатывается.\n"
        "-- DDL идёт ДВАЖДЫ — второй раз проверяет идемпотентность (init_db\n"
        "-- выполняется при каждом старте контейнера).\n"
        "BEGIN;\n\n" + ddl + ";\n\n"
        "-- второй прогон: те же команды, ошибок быть не должно\n" + ddl + ";\n\n"
        "-- что получилось\n"
        "SELECT column_name, data_type FROM information_schema.columns\n"
        " WHERE table_schema='public' AND table_name='" + ТАБЛИЦА + "'\n"
        " ORDER BY ordinal_position;\n"
        "SELECT indexname FROM pg_indexes\n"
        " WHERE schemaname='public' AND tablename='" + ТАБЛИЦА + "';\n\n"
        "ROLLBACK;\n\n"
        "-- контрольный замер ПОСЛЕ отката: обязано вернуть пусто\n"
        "SELECT to_regclass('public." + ТАБЛИЦА + "');\n\n"
        "-- ТОЧКА ОТКАТА (выполняется руками и только если таблицу решат убрать):\n"
        "-- " + откат.replace("\n", "\n-- ") + ";\n"
    )


if __name__ == "__main__":
    if "--sql" in sys.argv:
        print(SQL_ТЕКСТОМ())
        sys.exit(0)
    sys.exit(asyncio.run(main()))
