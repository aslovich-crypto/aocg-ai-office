# -*- coding: utf-8 -*-
"""Readiness-ручка /health/db: жива ли база, а не только веб-процесс.

Живёт в живом контуре НАМЕРЕННО: на FakePool этот тест был бы
самообманом в квадрате — двойник «жив» всегда, и обе ветки ручки
проверялись бы на приборе, который не умеет умирать.

Ручка без авторизации, как и /health: у монитора нет токена. Наружу
уходит только слово (ok / db_unavailable) — ни версии, ни текста
ошибки, ни тем более DSN.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


async def _монитор():
    """Клиент БЕЗ авторизации — как ходит настоящий UptimeRobot."""
    app.dependency_overrides.clear()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_живая_база_даёт_200(db):
    async with await _монитор() as c:
        r = await c.get("/health/db")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_мёртвая_база_даёт_503_и_не_выдаёт_причину(db):
    """Ради этой половины ручка и писалась: /health на мёртвой базе зелёный.

    Базу убиваем ПО-НАСТОЯЩЕМУ — закрываем пул: ручка получает от
    get_pool() закрытый пул, и SELECT 1 падает так же, как упал бы
    при погасшем PostgreSQL.
    """
    await db.pool.close()
    async with await _монитор() as c:
        r = await c.get("/health/db")
    assert r.status_code == 503, r.text
    assert r.json() == {"status": "db_unavailable"}
    текст = r.text.lower()
    assert "postgres" not in текст and "dsn" not in текст, (
        "наружу не должно уходить ничего, кроме слова"
    )


@pytest.mark.asyncio
async def test_head_тоже_отвечает(db):
    """Мониторы ходят HEAD-ом — урок T46 (полтора месяца ложного DOWN)."""
    async with await _монитор() as c:
        r = await c.head("/health/db")
    assert r.status_code == 200, r.text
