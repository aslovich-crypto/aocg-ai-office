"""ОПОРА ДЛЯ ЗАМЕРА hook_probe.py. НЕ ЗАПУСКАТЬ — это данные, а не инструмент.

Случай B13: скрипт удаляет строки боевой базы и НЕ содержит имени хостера.
Прежний сторож ловил такие файлы словом «railway» в тексте; новый обязан
ловить их по существу — драйвер подключения плюс деструктивный глагол.
"""

import asyncpg


async def очистить(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    await conn.execute("DELETE FROM user_consents WHERE user_id = 'local_user'")
    await conn.close()
