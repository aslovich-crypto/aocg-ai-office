"""Чужая организация: проверки, которых не было, потому что зеркало врало (T35).

Замер `tests/tools/mirror_gaps.py` нашёл пять веток FakePool, которые
игнорировали условия доступа, стоящие в SQL роутера. Ветки исправлены —
но само по себе это ничего не доказывает: пока не было ТЕСТА, снятие
защиты в роутере оставалось незамеченным. Проверено мутациями: все пять
снимались, прогон оставался зелёным.

Здесь тесты на четыре из пяти. Пятый случай (состав отчётов,
`SELECT ri.* … JOIN reports r … WHERE r.org_id=$1`) тестом НЕ закрывается,
и это записано честно: `report_id` глобально уникален, поэтому связи чужого
отчёта не могут «прилипнуть» к своему — ответ не меняется, даже если фильтр
убрать. Это защита в глубину, её снятие через API не наблюдаемо. Зеркало
теперь честное, но красным оно не станет — так и написано в T35.
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


def _юзер(db, *, id=7, active=True, org_id=1):
    db.users.append(
        dict(
            id=id,
            first_name="Иван",
            last_name="Петров",
            email=f"u{id}@example.com",
            role="admin",
            org_id=org_id,
            is_active=active,
            is_email_verified=True,
            password_hash=None,
        )
    )
    return db.users[-1]


# ───────────── ① отключённый пользователь и его живой токен ─────────────────


@pytest.mark.asyncio
async def test_deactivated_user_token_stops_working(db):
    """Отключили человека — токен на руках перестаёт работать.

    Пока ветка FakePool не читала `AND is_active=true`, это НЕ проверялось
    ничем: любой тест деактивации (в том числе S-29, где мы её закрывали)
    доказывал лишь то, что флаг в базе перевернулся.
    """
    _юзер(db, id=7, active=False)
    async with await _без_подмены(create_access_token(7)) as c:
        r = await c.get("/api/cards/")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_active_user_token_works(db):
    """Положительная половина: живой пользователь ходит как раньше."""
    _юзер(db, id=8, active=True)
    async with await _без_подмены(create_access_token(8)) as c:
        r = await c.get("/api/cards/")
    assert r.status_code == 200, r.text


# ──────────────────── ② переименование чужой карты ──────────────────────────


@pytest.mark.asyncio
async def test_card_of_other_org_cannot_be_renamed(client, db, seeded):
    db.cards.append(dict(id=42, name="Чужая", org_id=ЧУЖАЯ, created_at=None))
    r = await client.patch("/api/cards/42", json={"name": "Переименована"})
    assert r.status_code == 404, r.text
    (чужая,) = [c for c in db.cards if c["id"] == 42]
    assert чужая["name"] == "Чужая", "карта чужой орг не должна была измениться"


# ─────────────── ③ подсказка способа оплаты из чужой орг ────────────────────


@pytest.mark.asyncio
async def test_payment_hint_never_comes_from_other_org(client, db):
    """Подсказка считается ТОЛЬКО по своим чекам.

    Здесь утечка была бы не гипотетической: по названию контрагента
    подсказывался бы способ оплаты, которым платит ЧУЖАЯ организация.
    """
    from datetime import date

    def чек(id, org_id, payment):
        db.receipts.append(
            dict(
                id=id,
                date=date(2026, 5, 10),
                org="Лукойл",
                amount=100.0,
                payment=payment,
                org_id=org_id,
                employee=None,
                kkt_fn=None,
                raw_data=None,
                source="manual",
                photo_url=None,
                user_id=1,
                created_at=None,
            )
        )

    чек(90, ЧУЖАЯ, "Чужая карта")
    чек(91, ЧУЖАЯ, "Чужая карта")

    пусто = await client.get("/api/receipts/suggest-payment?org=Лукойл")
    assert пусто.status_code == 200, пусто.text
    assert пусто.json()["payment"] is None, (
        "своих чеков нет — подсказки быть не должно, а не «как у соседей»"
    )

    чек(1, 1, "Наличные")
    свой = await client.get("/api/receipts/suggest-payment?org=Лукойл")
    assert свой.json()["payment"] == "Наличные", (
        "подсказка обязана считаться по своим чекам, даже если чужих больше"
    )


# ──────────────── ⑤ смена статуса чужого отчёта ─────────────────────────────


@pytest.mark.asyncio
async def test_status_of_other_org_report_cannot_be_changed(client, db, seeded):
    from datetime import date

    db.reports.append(
        dict(
            id=90,
            title="Чужой отчёт",
            status="Черновик",
            total=0,
            org_id=ЧУЖАЯ,
            created=date(2026, 5, 1),
            created_at=None,
            user_id=99,
        )
    )
    # Статус берётся из списка допустимых («Одобрен», не «Утверждён») —
    # иначе 422 от валидации приходит РАНЬШЕ проверки org-scope, и тест
    # доказывал бы работу Pydantic, а не изоляции организаций.
    r = await client.patch("/api/reports/90", json={"status": "Одобрен"})
    assert r.status_code == 404, r.text
    (чужой,) = [x for x in db.reports if x["id"] == 90]
    assert чужой["status"] == "Черновик", "статус чужого отчёта не должен меняться"
