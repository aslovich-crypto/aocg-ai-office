# -*- coding: utf-8 -*-
"""Чужая организация и живой токен — НА ЖИВОЙ БАЗЕ (T35 → T36, сессия 2).

ПЕРЕВЕДЕНО С FakePool 03.09.2026. История файла: замер
`tests/tools/mirror_gaps.py` нашёл пять веток FakePool, которые
игнорировали условия доступа, стоящие в SQL роутера (T35). Ветки тогда
исправили и закрыли тестами — но исправленное зеркало так и осталось
ТОЛКОВАТЕЛЕМ запроса. Здесь SQL исполняется настоящим PostgreSQL,
и толковать больше нечего.

Пятый случай T35 (состав отчётов, `SELECT ri.* … JOIN reports r …
WHERE r.org_id=$1`) тестом НЕ закрывается, и это записано честно:
`report_id` глобально уникален, поэтому связи чужого отчёта не могут
«прилипнуть» к своему — ответ не меняется, даже если фильтр убрать.
Это защита в глубину, её снятие через API не наблюдаемо.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import create_access_token
from app.main import app

ЧУЖАЯ = 2


async def _без_подмены(токен=None):
    """Клиент БЕЗ подмены get_current_user — с настоящим Bearer-токеном.

    Нужен там, где проверяется САМА авторизация: обычные фикстуры
    подменяют зависимость и до неё дело не доходит.
    """
    app.dependency_overrides.clear()
    заголовки = {"Authorization": f"Bearer {токен}"} if токен else {}
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=заголовки
    )


# ───────────── ① отключённый пользователь и его живой токен ─────────────────


@pytest.mark.asyncio
async def test_deactivated_user_token_stops_working(db):
    """Отключили человека — токен на руках перестаёт работать.

    Пока ветка FakePool не читала `AND is_active=true`, это НЕ проверялось
    ничем: любой тест деактивации (в том числе S-29, где мы её закрывали)
    доказывал лишь то, что флаг в базе перевернулся. Теперь условие
    исполняет сам PostgreSQL.
    """
    await db.добавить_пользователя(
        id=7, first_name="Иван", role="admin", is_active=False
    )
    async with await _без_подмены(create_access_token(7)) as c:
        r = await c.get("/api/cards/")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_active_user_token_works(db):
    """Положительная половина: живой пользователь ходит как раньше."""
    await db.добавить_пользователя(id=8, first_name="Иван", role="admin")
    async with await _без_подмены(create_access_token(8)) as c:
        r = await c.get("/api/cards/")
    assert r.status_code == 200, r.text


# ──────────────────── ② переименование чужой карты ──────────────────────────


@pytest.mark.asyncio
async def test_card_of_other_org_cannot_be_renamed(client, db, seeded):
    await db.добавить_карту(id=42, name="Чужая", org_id=ЧУЖАЯ)
    r = await client.patch("/api/cards/42", json={"name": "Переименована"})
    assert r.status_code == 404, r.text
    assert (await db.карта(42))["name"] == "Чужая", (
        "карта чужой орг не должна была измениться"
    )


# ─────────────── ③ подсказка способа оплаты из чужой орг ────────────────────


@pytest.mark.asyncio
async def test_payment_hint_never_comes_from_other_org(client, db):
    """Подсказка считается ТОЛЬКО по своим чекам.

    Здесь утечка была бы не гипотетической: по названию контрагента
    подсказывался бы способ оплаты, которым платит ЧУЖАЯ организация.
    """
    from datetime import date

    # Автор чека — настоящий FK: смотрящий (id=1) должен существовать
    # в обеих организациях... нет — user_id один, организации у ЧЕКОВ разные
    # (у receipts.org_id внешнего ключа нет, как и в проде).
    await db.добавить_пользователя(id=1, first_name="Админ", role="admin")

    async def чек(id, org_id, payment):
        await db.добавить_чек(
            id=id,
            org="Лукойл",
            amount=100.0,
            date=date(2026, 5, 10),
            payment=payment,
            org_id=org_id,
            user_id=1,
        )

    await чек(90, ЧУЖАЯ, "Чужая карта")
    await чек(91, ЧУЖАЯ, "Чужая карта")

    пусто = await client.get("/api/receipts/suggest-payment?org=Лукойл")
    assert пусто.status_code == 200, пусто.text
    assert пусто.json()["payment"] is None, (
        "своих чеков нет — подсказки быть не должно, а не «как у соседей»"
    )

    await чек(1, 1, "Наличные")
    свой = await client.get("/api/receipts/suggest-payment?org=Лукойл")
    assert свой.json()["payment"] == "Наличные", (
        "подсказка обязана считаться по своим чекам, даже если чужих больше"
    )


# ──────────────── ⑤ смена статуса чужого отчёта ─────────────────────────────


@pytest.mark.asyncio
async def test_status_of_other_org_report_cannot_be_changed(client, db, seeded):
    from datetime import date

    # Автор чужого отчёта обязан существовать (reports.user_id — FK),
    # и он живёт в чужой организации: добавить_пользователя сам заведёт орг 2.
    await db.добавить_пользователя(
        id=99, first_name="Чужой", role="admin", org_id=ЧУЖАЯ
    )
    await db.добавить_отчёт(
        id=90,
        title="Чужой отчёт",
        status="Черновик",
        total=0,
        org_id=ЧУЖАЯ,
        user_id=99,
        created=date(2026, 5, 1),
    )
    # Статус берётся из списка допустимых («Одобрен», не «Утверждён») —
    # иначе 422 от валидации приходит РАНЬШЕ проверки org-scope, и тест
    # доказывал бы работу Pydantic, а не изоляции организаций.
    r = await client.patch("/api/reports/90", json={"status": "Одобрен"})
    assert r.status_code == 404, r.text
    assert (await db.отчёт(90))["status"] == "Черновик", (
        "статус чужого отчёта не должен меняться"
    )
