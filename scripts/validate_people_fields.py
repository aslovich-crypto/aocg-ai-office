#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ВАЛИДАЦИЯ МИГРАЦИИ «обязательные поля человека» — BEGIN/ROLLBACK, БЕЗ ЗАПИСИ.

⚠️ ЗАКРЕПЛЯЮЩЕЙ КОМАНДЫ ЗДЕСЬ НЕТ НИ ОДНОЙ — только откат. Транзакция
открывается, DDL прогоняется на ЖИВОЙ схеме, результат замеряется — и всё
откатывается. База остаётся ровно такой же, какой была.

⚠️ СПИСОК ОДИН — ИНАЧЕ ПРОВЕРЯЛИ БЫ ОДНО, А ПРИМЕНЯЛИ ДРУГОЕ. Те же строки
дословно лежат в `init_db()`; совпадение стережёт
`tests/test_migration_people_fields.py`.

ВАЛИДАЦИЯ НА ПРОДЕ ПРОЙДЕНА 08.09.2026 (владелец, бастион): UPDATE 0 трижды
(пустых полей нет), шесть ALTER TABLE без ошибок, сверка 4 · 0 · 0 · 0,
ROLLBACK выполнен.

ЗАПУСК (с бастиона, где есть DATABASE_URL):
    python3 scripts/validate_people_fields.py
"""

import asyncio
import os
import sys

import asyncpg

ТАБЛИЦА = "users"

# ⚠️ ПОЧЕМУ CHECK, А НЕ ОДИН NOT NULL. `NOT NULL` пропускает ПУСТУЮ СТРОКУ:
# она не NULL. Регистрация до 08.09.2026 писала именно `""`, а не NULL, —
# то есть один `NOT NULL` не закрыл бы ровно тот случай, ради которого всё
# и затевалось.
#
# ⚠️ ПОЧЕМУ DROP IF EXISTS + ADD, А НЕ «ADD CONSTRAINT IF NOT EXISTS».
# Такой формы в PostgreSQL НЕТ, а `init_db()` крутится на КАЖДОМ старте:
# голый `ADD CONSTRAINT` со второго запуска падал бы и ронял приложение
# целиком (класс T89). Цена приёма названа честно: при каждом старте
# ограничение пересоздаётся, то есть таблица перепроверяется целиком.
# На нашем размере (4 строки на 08.09.2026) это доли миллисекунды; на
# большой таблице приём пришлось бы менять на блок `DO ... IF NOT EXISTS`.
МИГРАЦИЯ = [
    "ALTER TABLE users ALTER COLUMN first_name SET NOT NULL",
    "ALTER TABLE users ALTER COLUMN last_name  SET NOT NULL",
    "ALTER TABLE users ALTER COLUMN email      SET NOT NULL",
    "ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_first_name_notblank",
    "ALTER TABLE users ADD  CONSTRAINT ck_users_first_name_notblank CHECK (btrim(first_name) <> '')",
    "ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_last_name_notblank",
    "ALTER TABLE users ADD  CONSTRAINT ck_users_last_name_notblank  CHECK (btrim(last_name)  <> '')",
    "ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_email_notblank",
    "ALTER TABLE users ADD  CONSTRAINT ck_users_email_notblank      CHECK (btrim(email)      <> '')",
]

# ⚠️ ИНДЕКС СТАНОВИТСЯ ОБЫЧНЫМ — И ТОЛЬКО ПОСЛЕ ТОГО, КАК ВСТАЛ CHECK.
# Частичность заводили потому, что пустая почта была возможна. Теперь она
# невозможна по построению, и условие `WHERE email IS NOT NULL AND email <> ''`
# истинно всегда — то есть частичность стала украшением.
#
# ⚠️ НОВОЕ ИМЯ, А НЕ ПЕРЕСОЗДАНИЕ ПОД СТАРЫМ. `CREATE ... IF NOT EXISTS` со
# старым именем ничего бы не сделал (индекс существует), а `DROP` + `CREATE`
# на каждом старте перестраивал бы индекс заново — дорого и без нужды.
# С новым именем обе строки идемпотентны: после первого прохода первая — no-op,
# вторая — no-op. Порядок «сперва создать, потом снять» держит уникальность
# без разрыва.
ИНДЕКСЫ = [
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower_all ON users (lower(email))",
    "DROP INDEX IF EXISTS uq_users_email_lower",
]


async def состояние(conn) -> dict:
    """Что есть сейчас: обязательность колонок, ограничения, индексы."""
    колонки = {
        r["column_name"]: r["is_nullable"]
        for r in await conn.fetch(
            """SELECT column_name, is_nullable FROM information_schema.columns
               WHERE table_name=$1 AND column_name = ANY($2)""",
            ТАБЛИЦА,
            ["first_name", "last_name", "email"],
        )
    }
    ограничения = {
        r["conname"]
        for r in await conn.fetch(
            """SELECT conname FROM pg_constraint
               WHERE conrelid = $1::regclass AND contype = 'c'""",
            ТАБЛИЦА,
        )
    }
    индексы = {
        r["indexname"]
        for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename=$1", ТАБЛИЦА
        )
    }
    return {"колонки": колонки, "ограничения": ограничения, "индексы": индексы}


async def main() -> int:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL не задан — валидировать нечего")
        return 1
    conn = await asyncpg.connect(dsn)
    try:
        было = await состояние(conn)
        строк_было = await conn.fetchval(f"SELECT count(*) FROM {ТАБЛИЦА}")
        пустых = await conn.fetchrow(
            """SELECT count(*) FILTER (WHERE btrim(coalesce(first_name,''))='') AS имя,
                      count(*) FILTER (WHERE btrim(coalesce(last_name,''))='')  AS фамилия,
                      count(*) FILTER (WHERE btrim(coalesce(email,''))='')      AS почта
               FROM users"""
        )
        print(f"ЧТО БЫЛО: строк {строк_было}, пустых {dict(пустых)}")
        print(f"          nullable {было['колонки']}")

        tr = conn.transaction()
        await tr.start()
        try:
            for ddl in МИГРАЦИЯ + ИНДЕКСЫ:
                await conn.execute(ddl)
            стало = await состояние(conn)
            строк_стало = await conn.fetchval(f"SELECT count(*) FROM {ТАБЛИЦА}")
            print(f"ЧТО СТАЛО: строк {строк_стало}, nullable {стало['колонки']}")
            print(f"          ограничений {sorted(стало['ограничения'])}")
            print(f"          индексов {sorted(стало['индексы'])}")
        finally:
            await tr.rollback()

        осталось = await состояние(conn)
        print(f"ПОСЛЕ ОТКАТА: nullable {осталось['колонки']}")
        сошлось = осталось == было
        print("✓ откат вернул схему как было" if сошлось else "✗ СХЕМА ИЗМЕНИЛАСЬ")
        return 0 if сошлось else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
