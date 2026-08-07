#!/usr/bin/env bash
# ОПОРА ДЛЯ ЗАМЕРА hook_probe.py. НЕ ЗАПУСКАТЬ.
# Случай B5: деструктив вынесен из строки команды в .sh — прежний сторож
# в такие файлы не смотрел, и `bash cleanup.sh` проходил насквозь.
railway run --service Postgres ./venv/bin/python -c "
import asyncio, asyncpg, os
async def main():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    await conn.execute('DELETE FROM user_consents')
asyncio.run(main())
"
