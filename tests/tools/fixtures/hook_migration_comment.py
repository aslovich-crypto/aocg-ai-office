"""ОПОРА ДЛЯ ЗАМЕРА hook_probe.py. НЕ ЗАПУСКАТЬ — это данные, а не миграция.

Случай A17: миграция, оформленная ПО РЕГЛАМЕНТУ репозитория — прямой DDL
исполняется, обратный записан комментарием рядом. Сторож обязан её пускать:
комментарий исполнен не будет. До T5 он блокировал такие файлы, то есть
наказывал ровно за соблюдение правила.

Обратный DDL намеренно НЕ вынесен в этот docstring: строка — не комментарий,
её можно передать в execute, и сторож обязан её видеть.
"""

import os

import asyncpg


async def применить() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_receipts_org ON receipts(org_id)"
    )
    # откат: DROP INDEX IF EXISTS idx_receipts_org;
    await conn.close()
