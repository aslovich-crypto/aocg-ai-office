"""ОПОРА ДЛЯ ЗАМЕРА hook_probe.py. НЕ ЗАПУСКАТЬ — это данные.

Случай B15 — ОБРАТНЫЙ к A17 и главный довод за токенизатор.
Решётка стоит ВНУТРИ строкового литерала и ПЕРЕД глаголом на той же
строке. Python её не выкинет — строка уйдёт в execute целиком. Наивное
снятие «от # до конца строки» срезало бы вместе с ней и запрос, и сторож
пропустил бы настоящее удаление, приняв его за комментарий.
"""

import os

import asyncpg

ЗАПРОС = "SET search_path = public; #пометка миграции; DELETE FROM user_consents"


async def применить() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    await conn.execute(ЗАПРОС)
    await conn.close()
