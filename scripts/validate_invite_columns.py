#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ВАЛИДАЦИЯ МИГРАЦИИ invite_links — BEGIN/ROLLBACK, БЕЗ ЗАПИСИ (T104, этап ①).

⚠️ ЗАКРЕПЛЯЮЩЕЙ КОМАНДЫ ЗДЕСЬ НЕТ НИ ОДНОЙ — только откат.
(Слово из семи букв не пишу даже в пояснении: грепом по файлу его
быть не должно, иначе проверка «нет ли закрепления» ловит сама себя.)
Транзакция открывается,
DDL прогоняется на ЖИВОЙ схеме, результат замеряется — и всё
откатывается. База остаётся ровно такой же, какой была.

⚠️ ЗАЧЕМ ВООБЩЕ ПРОГОНЯТЬ, ЕСЛИ DDL ШАБЛОННЫЙ. Затем, что «шаблонный»
проверяется замером, а не верой: колонка может уже существовать с другим
типом, таблица — быть под блокировкой, прав может не хватить. Всё это
выясняется здесь, а не в момент запуска приложения перед сотрудниками.

ЗАПУСК (с бастиона, где есть DATABASE_URL):
    python3 scripts/validate_invite_columns.py

Что печатает: ЧТО БЫЛО → ЧТО СТАЛО → ЧТО ОСТАЛОСЬ ПОСЛЕ ОТКАТА.
Третье обязано совпасть с первым, иначе миграцию применять нельзя.
"""

import asyncio
import os
import sys

import asyncpg

ТАБЛИЦА = "invite_links"

# ⚠️ ТЕ ЖЕ ЧЕТЫРЕ СТРОКИ, ЧТО ПОЕДУТ В init_db. Список один — иначе
# проверяли бы одно, а применяли другое.
МИГРАЦИЯ = [
    f"ALTER TABLE {ТАБЛИЦА} ADD COLUMN IF NOT EXISTS email      TEXT",
    f"ALTER TABLE {ТАБЛИЦА} ADD COLUMN IF NOT EXISTS first_name TEXT",
    f"ALTER TABLE {ТАБЛИЦА} ADD COLUMN IF NOT EXISTS last_name  TEXT",
    f"ALTER TABLE {ТАБЛИЦА} ADD COLUMN IF NOT EXISTS sent_at    TIMESTAMPTZ",
]
ОЖИДАЕМЫЕ = {"email", "first_name", "last_name", "sent_at"}

# ⚠️ ИНДЕКСЫ ЛЕЖАТ ЗДЕСЬ ЖЕ, И ЭТО НЕ РАСШИРЕНИЕ ОБЛАСТИ ФАЙЛА, А ЕЁ СУТЬ:
# один список — один источник для валидации, для init_db и для теста
# совпадения копий. Развести их по разным файлам значило бы завести
# второй источник ровно того класса, который ловит T109.
#
# ⚠️ ПОЧЕМУ ИНДЕКС ЧАСТИЧНЫЙ. `UserCreate.email: str = ""` позволяет завести
# человека без почты, и таких строк может быть много — все они равны между
# собой. Обычный UNIQUE разрешил бы РОВНО ОДНОГО безпочтового на всю базу,
# а второго отверг бы при заведении: форма падала бы снова, теперь уже
# из-за нашей же защиты. NULL индекс пропускает сам, пустую строку
# приходится исключать явно — для базы это значение, а не отсутствие.
#
# ⚠️ IF NOT EXISTS ОБЯЗАТЕЛЕН: init_db крутится на КАЖДОМ старте, и упавшее
# создание индекса уронило бы запуск целиком (класс T89).
ИНДЕКСЫ = [
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower "
    "ON users (lower(email)) WHERE email IS NOT NULL AND email <> ''",
]


async def колонки(conn) -> set:
    строки = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name=$1",
        ТАБЛИЦА,
    )
    return {с["column_name"] for с in строки}


async def main() -> int:
    адрес = os.environ.get("DATABASE_URL")
    if not адрес:
        print("⚠️ ВАЛИДАЦИЯ НЕ ВЫПОЛНЕНА: не задана DATABASE_URL")
        print("   Запускать с бастиона, где переменная есть.")
        return 1

    conn = await asyncpg.connect(адрес)
    try:
        было = await колонки(conn)
        строк_было = await conn.fetchval(f"SELECT count(*) FROM {ТАБЛИЦА}")
        print(f"\nВАЛИДАЦИЯ МИГРАЦИИ {ТАБЛИЦА} (T104, этап ①)")
        print(f"  БЫЛО колонок {len(было)}: {', '.join(sorted(было))}")
        print(f"  строк в таблице: {строк_было}")

        лишние = ОЖИДАЕМЫЕ & было
        if лишние:
            print(f"  ⚠️ УЖЕ ЕСТЬ: {', '.join(sorted(лишние))}")
            print("     Миграция применена частично — план этапа ① меняется.")

        tr = conn.transaction()
        await tr.start()
        применилось = True
        try:
            for ddl in МИГРАЦИЯ:
                await conn.execute(ddl)
            стало = await колонки(conn)
            строк_стало = await conn.fetchval(f"SELECT count(*) FROM {ТАБЛИЦА}")
            print(f"  СТАЛО колонок {len(стало)}: {', '.join(sorted(стало))}")
            print(f"  строк в таблице: {строк_стало}")

            не_добавились = ОЖИДАЕМЫЕ - стало
            if не_добавились:
                применилось = False
                print(f"  ✗ НЕ ДОБАВИЛИСЬ: {', '.join(sorted(не_добавились))}")
            if строк_стало != строк_было:
                применилось = False
                print(f"  ✗ ЧИСЛО СТРОК ИЗМЕНИЛОСЬ: {строк_было} → {строк_стало}")

            # ⚠️ проверяем, что старые записи читаются и новые поля пусты,
            # а не заполнены чем-то по умолчанию
            проба = await conn.fetchrow(
                f"SELECT token, email, first_name, last_name, sent_at "
                f"FROM {ТАБЛИЦА} ORDER BY id LIMIT 1"
            )
            if проба is not None:
                пусты = all(
                    проба[к] is None
                    for к in ("email", "first_name", "last_name", "sent_at")
                )
                print(
                    f"  старые строки читаются, новые поля пусты: {'да' if пусты else 'НЕТ'}"
                )
                if not пусты:
                    применилось = False
        finally:
            # ⚠️ ОТКАТ ВСЕГДА, даже если выше что-то упало.
            await tr.rollback()

        осталось = await колонки(conn)
        print(f"  ПОСЛЕ ОТКАТА колонок {len(осталось)}: {', '.join(sorted(осталось))}")
        откат_чист = осталось == было
        print(f"  откат вернул схему как было: {'да' if откат_чист else 'НЕТ'}")

        if применилось and откат_чист:
            print("\n  ИТОГ: миграция применима, база не изменена")
            return 0
        print("\n  ⚠️ ИТОГ: применять НЕЛЬЗЯ — см. отметки ✗ выше")
        return 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
