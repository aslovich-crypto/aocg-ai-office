# -*- coding: utf-8 -*-
"""Рельсы валидации ключей интеграции — ПРОГОН НА ЖИВОЙ БАЗЕ.

⚠️ ЗАЧЕМ ОБЁРТКА, А НЕ ПРОСТО СКРИПТ. Рельсы, которые никто не запускает,
молчат о собственной поломке ровно так же, как молчали бы исправные.
У прошлых миграций это стоило отдельного разбора: рельсы `validate_people_fields`
и `validate_invite_columns` живут в репозитории и в прогон не попадают ни разу.

⚠️ И ГЛАВНОЕ: ТАБЛИЦА СНОСИТСЯ ПЕРЕД ПРОГОНОМ НАМЕРЕННО. `init_db` уже создал
её при подъёме схемы, и без сноса проверялся бы только ПОВТОРНЫЙ запуск —
путь «создаётся с нуля» остался бы непроверенным, а прогон выглядел бы
зелёным. Ровно на этом первый прогон 12.09.2026 и был забракован.
"""

import os
import subprocess
import sys

import pytest

СНЕСТИ = (
    "DROP INDEX IF EXISTS idx_integration_keys_org",
    "DROP INDEX IF EXISTS integration_keys_prefix_unique",
    "DROP TABLE IF EXISTS integration_keys",
)


async def _снести(db):
    for ddl in СНЕСТИ:
        await db.pool.execute(ddl)


def _прогон(dsn):
    итог = subprocess.run(
        [sys.executable, "scripts/validate_integration_keys.py"],
        env={**os.environ, "DATABASE_URL": dsn},
        capture_output=True,
        text=True,
        timeout=120,
    )
    return итог.returncode, итог.stdout + итог.stderr


@pytest.mark.asyncio
async def test_init_db_действительно_создаёт_таблицу(db):
    """Первая половина пары: схему поднимал НАСТОЯЩИЙ init_db.

    Без этой проверки зелёные рельсы ничего не говорят о проде: они создают
    таблицу своим списком внутри своей транзакции и прошли бы даже тогда,
    когда в init_db этого блока нет вовсе.
    """
    колонки = await db.pool.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='integration_keys'"
    )
    assert колонки, "init_db не создал таблицу integration_keys"
    имена = {с["column_name"] for с in колонки}
    # Три колонки, ради которых таблица и заводилась: чья, чем проверять, кто ходил.
    assert {"org_id", "secret_hash", "last_used_at"} <= имена


@pytest.mark.asyncio
async def test_рельсы_проходят_на_чистой_схеме(db, живая_схема):
    """Вторая половина: DDL годен, идемпотентен, откат сработал."""
    await _снести(db)
    код, вывод = _прогон(живая_схема)
    assert "ИТОГ" in вывод, "прибор не отработал вовсе:\n%s" % вывод[-800:]
    assert код == 0, "валидация не прошла:\n%s" % вывод
    assert "ИДЕМПОТЕНТНОСТЬ       : состав не изменился" in вывод
    # ⚠️ ОТКАТ ПРОВЕРЯЕТСЯ ЗАМЕРОМ ПОСЛЕ НЕГО, а не словами скрипта о себе.
    assert "ПРОВЕРКА ОТКАТА: таблица integration_keys отсутствует" in вывод


@pytest.mark.asyncio
async def test_после_валидации_база_не_изменилась(db, живая_схема):
    """Заведомо разная пара: до прогона таблицы нет — после него тоже нет.

    Совпади показания «есть/есть», прибор мерил бы не то: значит транзакция
    закрепилась, и проверочный прогон стал бы миграцией.
    """
    await _снести(db)
    было = await db.pool.fetchval("SELECT to_regclass('public.integration_keys')")
    _прогон(живая_схема)
    стало = await db.pool.fetchval("SELECT to_regclass('public.integration_keys')")
    assert было is None and стало is None, (
        "проверочный прогон оставил след в базе: было %s, стало %s" % (было, стало)
    )
