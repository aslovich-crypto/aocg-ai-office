"""Хвосты контура доступа: подтверждение почты, ЕГРЮЛ, чистка дублей (S-31).

Три ручки из тех, что замер покрытия (`tests/tools/cover_routes.py`)
показывал невыполненными: `GET /auth/verify-email`, `GET /egrul/{inn}`
и `POST /receipts/dedupe-cleanup/` (последняя доходила только до 403 —
то есть проверялся гейт и не проверялось то, что он охраняет).

СЕТЬ НАРУЖУ НЕ ХОДИТ: у ЕГРЮЛ подменяется httpx.AsyncClient. Тест,
который лезет в интернет, ломается не тогда, когда ломается код.
"""

import json
from datetime import date

import pytest

import app.routers.auth as auth_router


# ──────────────────────── подтверждение почты ───────────────────────────────


@pytest.fixture
def неподтверждённый(db):
    db.organizations.append(dict(id=1, name="ООО Ромашка", inn=None, type="company"))
    row = dict(
        id=5,
        first_name="Пётр",
        last_name="Сидоров",
        email="petr@example.com",
        password_hash="хеш",
        role="admin",
        org_id=1,
        is_active=True,
        is_email_verified=False,
        email_verify_token="ключ-из-письма",
    )
    db.users.append(row)
    db._uid = 5
    return row


@pytest.mark.asyncio
async def test_verify_email_marks_and_burns_the_token(client, неподтверждённый):
    r = await client.get("/api/auth/verify-email?token=ключ-из-письма")
    assert r.status_code == 200, r.text
    assert r.json()["access_token"], "после подтверждения выдаётся доступ"
    assert неподтверждённый["is_email_verified"] is True
    assert неподтверждённый["email_verify_token"] is None, (
        "ссылка обязана гаснуть — иначе ею можно воспользоваться повторно"
    )


@pytest.mark.asyncio
async def test_verify_email_rejects_unknown_token(client, неподтверждённый):
    r = await client.get("/api/auth/verify-email?token=подобранный")
    assert r.status_code == 400, r.text
    assert "access_token" not in r.json()
    assert неподтверждённый["is_email_verified"] is False, (
        "чужая попытка не должна подтверждать аккаунт"
    )


# ─────────────────────────────── ЕГРЮЛ ──────────────────────────────────────


class _Ответ:
    def __init__(self, тело):
        self._тело = тело

    def json(self):
        return json.loads(json.dumps(self._тело))


class _КлиентЕГРЮЛ:
    """Подмена httpx.AsyncClient: отдаёт заготовленные ответы, в сеть не ходит."""

    вызовы = 0

    def __init__(self, ответы, **kwargs):
        self._ответы = ответы

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *a, **kw):
        type(self).вызовы += 1
        return _Ответ(self._ответы["post"])

    async def get(self, *a, **kw):
        type(self).вызовы += 1
        return _Ответ(self._ответы["get"])


@pytest.mark.asyncio
async def test_egrul_rejects_bad_inn_without_touching_network(client, monkeypatch):
    """Длина ИНН проверяется ДО запроса: 10 или 12 цифр, иначе null.

    Отдельная ценность — не ходить наружу зря: сеть тут ещё и платная
    по времени (таймаут 8 секунд на запрос).
    """
    _КлиентЕГРЮЛ.вызовы = 0
    monkeypatch.setattr(
        auth_router.httpx, "AsyncClient", lambda **kw: _КлиентЕГРЮЛ({}, **kw)
    )
    for кривой in ("123", "abc", "12345678901"):
        r = await client.get(f"/api/egrul/{кривой}")
        assert r.status_code == 200, r.text
        assert r.json() is None, f"«{кривой}» не ИНН — ответ должен быть null"
    assert _КлиентЕГРЮЛ.вызовы == 0, "в сеть при кривом ИНН ходить не должны"


@pytest.mark.asyncio
async def test_egrul_returns_company_on_success(client, monkeypatch):
    ответы = {
        "post": {"t": "токен"},
        "get": {
            "status": "ok",
            "rows": [{"c": "ООО Ромашка", "i": "7700000000", "o": "1027700000000"}],
        },
    }
    monkeypatch.setattr(
        auth_router.httpx, "AsyncClient", lambda **kw: _КлиентЕГРЮЛ(ответы, **kw)
    )
    r = await client.get("/api/egrul/7700000000")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "name": "ООО Ромашка",
        "inn": "7700000000",
        "ogrn": "1027700000000",
    }


@pytest.mark.asyncio
async def test_egrul_survives_upstream_failure(client, monkeypatch):
    """Внешний сервис лёг — отвечаем null, а не 500.

    Фронт на этот null рассчитывает: показывает ручной ввод. Пятисотка
    вместо null сломала бы регистрацию, а не только подсказку.
    """

    def падает(**kw):
        raise RuntimeError("egrul недоступен")

    monkeypatch.setattr(auth_router.httpx, "AsyncClient", падает)
    r = await client.get("/api/egrul/7700000000")
    assert r.status_code == 200, r.text
    assert r.json() is None


# ───────────────────────── чистка дублей ────────────────────────────────────


def _чек(db, id, org_id, день=10, сумма=100.0, org="Лукойл"):
    db.receipts.append(
        dict(
            id=id,
            date=date(2026, 5, день),
            amount=сумма,
            org=org,
            org_id=org_id,
            payment="Корп.карта",
            employee=None,
            kkt_fn=None,
            raw_data=None,
            source="manual",
            photo_url=None,
            user_id=1,
            created_at=None,
        )
    )


@pytest.mark.asyncio
async def test_dedupe_cleanup_keeps_the_oldest_and_spares_other_orgs(client, db):
    """Считает и удаляет — но ТОЛЬКО в своей организации.

    ОГОВОРКА ЧЕСТНАЯ: группировка дублей эмулируется в FakePool вручную,
    поэтому тест доказывает логику ХЕНДЛЕРА (что удаляется всё, кроме
    самого раннего, и что счётчики сходятся), а не правильность GROUP BY
    в SQL. Настоящий PostgreSQL — задача T36.
    """
    _чек(db, 1, org_id=1)
    _чек(db, 2, org_id=1)  # дубль первого
    _чек(db, 3, org_id=1, день=11)  # не дубль
    _чек(db, 90, org_id=2)
    _чек(db, 91, org_id=2)  # дубль, но ЧУЖОЙ

    r = await client.post("/api/receipts/dedupe-cleanup/")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 1, "kept": 1}

    остались = {x["id"] for x in db.receipts}
    assert 1 in остались and 2 not in остались, "оставаться должен самый ранний"
    assert 3 in остались, "одиночный чек не трогаем"
    assert {90, 91} <= остались, "дубли ЧУЖОЙ организации не наши — не трогаем"


@pytest.mark.asyncio
async def test_dedupe_cleanup_is_admin_only(client_employee, db):
    _чек(db, 1, org_id=1)
    _чек(db, 2, org_id=1)
    r = await client_employee.post("/api/receipts/dedupe-cleanup/")
    assert r.status_code == 403, r.text
    assert len(db.receipts) == 2, "ни один чек не должен был удалиться"
