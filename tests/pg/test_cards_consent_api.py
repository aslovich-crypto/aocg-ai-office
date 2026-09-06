# -*- coding: utf-8 -*-
"""Карты оплаты и журнал согласий — НА ЖИВОЙ БАЗЕ (T36, перевод, заход 3).

ПЕРЕВЕДЕНО С FakePool 06.09.2026, последний заход перевода. Десять тестов:
справочник карт (список, создание, удаление) и журнал согласий на обработку
персональных данных (кто субъект, откуда адрес, повторное согласие, выдача
последнего, изоляция между людьми).

⚠️ ПОЧЕМУ СОГЛАСИЯ ВАЖНЕЕ, ЧЕМ КАЖУТСЯ. `user_consents` — доказательный
журнал по 152-ФЗ: субъект берётся ИЗ ТОКЕНА, адрес — ИЗ СОЕДИНЕНИЯ, и ни то
ни другое клиент подсунуть не может. Девятнадцать легаси-строк «local_user»
на проде появились ровно потому, что значение когда-то приходило от клиента.
Проверять такое на зеркале, которое само же и решает, что записать, — значит
проверять собственное представление о записи, а не запись.
"""

import pytest

from app.routers.consent import POLICY_VERSION

# ⚠️ ФИКСТУРЫ СМОТРЯЩЕГО ЗДЕСЬ НЕТ — И ЭТО ЗАМЕР, А НЕ ЗАБЫВЧИВОСТЬ. В заходах
# 1 и 2 такая фикстура была обязательной: `receipts.user_id` и `reports.user_id`
# — настоящие внешние ключи, и без строки в `users` живая база отказывала. Здесь
# ключей нет: `cards.org_id` — просто INTEGER (`ALTER TABLE cards ADD COLUMN
# IF NOT EXISTS org_id INTEGER`), `user_consents.user_id` — TEXT NOT NULL без
# ссылки. Первая редакция файла фикстуру всё же завела «по образцу» и объяснила
# несуществующим FK; снял её и прогнал — все десять зелёные. Ссылаться на связь,
# которой нет, — тот же дефект, что писать в колонку, которой нет.


# ─── GET /api/cards/ ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_cards_returns_list(client, seeded):
    resp = await client.get("/api/cards/")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["name"] == "Корп.карта"


# ─── POST /api/cards/ ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_card(client):
    resp = await client.post("/api/cards/", json={"name": "Личная Сбер"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] > 0
    assert body["name"] == "Личная Сбер"


# ─── DELETE /api/cards/{id} ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_delete_card(client):
    created = await client.post("/api/cards/", json={"name": "Временная"})
    cid = created.json()["id"]

    resp = await client.delete(f"/api/cards/{cid}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    remaining = (await client.get("/api/cards/")).json()
    assert all(c["id"] != cid for c in remaining)


# ─── POST /api/consent/ ───────────────────────────────────────────────
# СТРОКА 9: субъект берётся ИЗ ТОКЕНА, адрес — ИЗ ЗАПРОСА. Клиент не может
# быть источником доказательства о самом себе, поэтому тела эти поля больше
# не несут (а если старый фронт их пришлёт — они игнорируются).
@pytest.mark.asyncio
async def test_post_consent_records_row(client, db):
    resp = await client.post("/api/consent/", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] > 0
    # S-34: версия берётся из ИСТОЧНИКА, а не литералом — иначе тест
    # становится третьей копией того же значения и расходится с ним.
    assert body["policy_version"] == POLICY_VERSION
    assert body["consent_at"] is not None
    журнал = await db.согласия()
    assert len(журнал) == 1
    # id=1 — это подменённый get_current_user в фикстуре client.
    assert журнал[0]["user_id"] == "1"
    assert "Шукалович" in журнал[0]["consent_text"]


@pytest.mark.asyncio
async def test_post_consent_ignores_subject_from_body(client, db):
    """Подсунуть чужой user_id через тело нельзя — иначе запись подделывается.

    Именно так и появились девятнадцать легаси-строк «local_user»: значение
    приходило от клиента, и журнал не опознаёт по ним никого.
    """
    resp = await client.post(
        "/api/consent/",
        json={"user_id": "local_user", "ip_address": "203.0.113.4"},
    )
    assert resp.status_code == 200
    журнал = await db.согласия()
    assert журнал[0]["user_id"] == "1", "субъект обязан приходить из токена"
    assert журнал[0]["ip_address"] != "203.0.113.4", (
        "адрес обязан браться из запроса, а не из тела"
    )


@pytest.mark.asyncio
async def test_post_consent_records_client_address(client, db):
    """Адрес пишется сервером. В тестах соединение локальное — важно, что
    поле ЗАПОЛНЕНО и взято не из тела."""
    await client.post("/api/consent/", json={})
    assert (await db.согласия())[0]["ip_address"], "адрес обязан проставиться"


@pytest.mark.asyncio
async def test_post_consent_appends_on_reagree(client, db):
    """Re-agreement is intentional — we append rather than upsert."""
    await client.post("/api/consent/", json={})
    await client.post("/api/consent/", json={})
    assert len(await db.согласия()) == 2


# ─── GET /api/consent/{user_id} ───────────────────────────────────────
@pytest.mark.asyncio
async def test_get_consent_returns_null_when_none(client):
    resp = await client.get("/api/consent/never_consented")
    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.asyncio
async def test_get_consent_returns_latest(client, db):
    await client.post("/api/consent/", json={})
    second = await client.post("/api/consent/", json={})
    resp = await client.get("/api/consent/1")
    assert resp.status_code == 200
    body = resp.json()
    # 'latest' = highest id, which the POST returned
    assert body["id"] == second.json()["id"]
    # S-34: версия берётся из ИСТОЧНИКА, а не литералом — иначе тест
    # становится третьей копией того же значения и расходится с ним.
    assert body["policy_version"] == POLICY_VERSION


@pytest.mark.asyncio
async def test_get_consent_isolates_users(client, db):
    await client.post("/api/consent/", json={})
    resp = await client.get("/api/consent/bob")
    assert resp.status_code == 200
    assert resp.json() is None
