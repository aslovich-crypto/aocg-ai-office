# -*- coding: utf-8 -*-
"""Сверка двух баз перед отключением Railway (S-06, хвост).

⚠️ ТОЛЬКО ЧТЕНИЕ ОБЕИХ БАЗ: каждое соединение открывает транзакцию
readonly=True — записать не даст сам PostgreSQL, а не наша дисциплина.
Команды фиксации в файле нет (проверяется глазами и грепом — слово
здесь не пишем, валидатор UNIQUE однажды споткнулся о собственную
докстроку).

ЧТО ДЕЛАЕТ: по каждой таблице обеих баз — число строк, наибольший id
(где он есть) и самая свежая дата создания. Таблицы берутся ИЗ САМИХ БАЗ
(information_schema), а не из списка в голове — чтобы забытая таблица
не потерялась молча (T87).

ЗАПУСК (с бастиона или с машины, откуда достижимы ОБЕ базы):

    RAILWAY_URL='postgresql://...'  TIMEWEB_URL='postgresql://...' \\
        python3 compare_databases.py

Откуда адреса: RAILWAY_URL — панель Railway → Postgres → Connect →
Database URL (публичный, с портом proxy.rlwy.net); TIMEWEB_URL — тот же
DATABASE_URL, что использовался для валидатора UNIQUE 31.08.2026.

КОДЫ ВЫХОДА: 0 — базы совпали по числам · 1 — есть расхождения ·
3 — проверка не выполнена (одна из баз не ответила).
"""

import asyncio
import os
import sys

ТАБЛИЦЫ_SQL = """
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name
"""


async def _снять(url, метка):
    import asyncpg

    conn = await asyncpg.connect(url, timeout=20)
    итог = {}
    try:
        async with conn.transaction(readonly=True):
            таблицы = [r["table_name"] for r in await conn.fetch(ТАБЛИЦЫ_SQL)]
            if not таблицы:
                print(f"✗ {метка}: таблиц не найдено — это поломка замера, не пустота")
                return None
            for т in таблицы:
                строк = await conn.fetchval(f'SELECT count(*) FROM "{т}"')
                наибольший = None
                свежесть = None
                колонки = {
                    r["column_name"]
                    for r in await conn.fetch(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=$1",
                        т,
                    )
                }
                if "id" in колонки:
                    наибольший = await conn.fetchval(f'SELECT max(id) FROM "{т}"')
                for к in ("created_at", "created", "sent_at"):
                    if к in колонки:
                        свежесть = await conn.fetchval(
                            f'SELECT max("{к}") FROM "{т}"'
                        )
                        break
                итог[т] = (строк, наибольший, свежесть)
    finally:
        await conn.close()
    return итог


async def _прогон():
    rw = os.environ.get("RAILWAY_URL", "").strip()
    tw = os.environ.get("TIMEWEB_URL", "").strip()
    if not rw or not tw:
        print("✗ ПРОВЕРКА НЕ ВЫПОЛНЕНА: задайте RAILWAY_URL и TIMEWEB_URL")
        return 3
    print("СВЕРКА БАЗ ПЕРЕД ОТКЛЮЧЕНИЕМ RAILWAY (только чтение)\n")
    try:
        старая = await _снять(rw, "Railway")
    except Exception as e:  # noqa: BLE001 — владельцу нужна причина, не трейс
        print(f"✗ ПРОВЕРКА НЕ ВЫПОЛНЕНА: Railway не ответил — {type(e).__name__}: {e}")
        return 3
    try:
        новая = await _снять(tw, "Timeweb")
    except Exception as e:  # noqa: BLE001
        print(f"✗ ПРОВЕРКА НЕ ВЫПОЛНЕНА: Timeweb не ответил — {type(e).__name__}: {e}")
        return 3
    if старая is None or новая is None:
        return 3

    все = sorted(set(старая) | set(новая))
    ш = max(len(т) for т in все)
    расхождений = 0
    print(f"  {'ТАБЛИЦА':<{ш}}  {'RAILWAY':>14}  {'TIMEWEB':>14}")
    for т in все:
        s = старая.get(т)
        n = новая.get(т)
        подпись_s = "—" if s is None else f"{s[0]}"
        подпись_n = "—" if n is None else f"{n[0]}"
        знак = "✓"
        # ⚠️ Расхождением считаем СТАРОЕ > НОВОГО: в Timeweb могло прибавиться
        # (там живут), а вот строки, которых в Timeweb МЕНЬШЕ, чем в Railway, —
        # кандидаты на потерянное при переезде. Про них и кричим.
        if s is not None and (n is None or s[0] > n[0]):
            знак = "✗ СТАРОЕ БОЛЬШЕ"
            расхождений += 1
        print(f"  {т:<{ш}}  {подпись_s:>14}  {подпись_n:>14}  {знак}")
        if s and s[2]:
            print(f"  {'':<{ш}}  свежайшая запись в Railway: {s[2]}")
    print()
    if расхождений:
        print(f"⚠️ РАСХОЖДЕНИЙ {расхождений}: в Railway есть строки, которых нет в Timeweb.")
        print("   НЕ ОТКЛЮЧАТЬ, пока не решено, нужны ли они. Снимите дамп (шаг ② порядка).")
        return 1
    print("ИТОГ: в Railway нет ничего, чего не было бы в Timeweb. Дамп всё равно снимите — он бесплатный.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_прогон()))
