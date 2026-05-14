"""API endpoint tests for AOCG AI Office.

Run against an in-memory fake pool (see conftest.py) — no real database is
touched. Each test gets a fresh store via the `db` / `seeded` fixtures.
"""


# ─── GET /api/receipts/ ───────────────────────────────────────────────
async def test_get_receipts_returns_list(client):
    resp = await client.get("/api/receipts/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_get_receipts_with_data(client, seeded):
    resp = await client.get("/api/receipts/")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["org"] == "Лукойл"


# ─── POST /api/receipts/ ──────────────────────────────────────────────
async def test_create_receipt(client):
    payload = {"date": "2026-05-14", "org": "Магнит", "amount": 1234.56,
               "payment": "Наличные"}
    resp = await client.post("/api/receipts/", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] > 0
    assert body["org"] == "Магнит"
    assert body["amount"] == 1234.56
    # auto-categorization: "Магнит" -> "Продукты"
    assert body["category"] == "Продукты"


# ─── POST /api/receipts/ with duplicate fn -> 409 ─────────────────────
async def test_create_receipt_duplicate_fn_returns_409(client):
    payload = {"date": "2026-05-14", "org": "Лукойл", "amount": 5000.0, "fn": "DUP-FN-123"}
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200

    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["error"] == "duplicate"
    assert detail["existing_id"] == first.json()["id"]


# ─── DELETE /api/receipts/{id} ────────────────────────────────────────
async def test_delete_receipt(client):
    created = await client.post("/api/receipts/", json={
        "date": "2026-05-14", "org": "ВкусВилл", "amount": 800.0})
    rid = created.json()["id"]

    resp = await client.delete(f"/api/receipts/{rid}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    remaining = (await client.get("/api/receipts/")).json()
    assert all(r["id"] != rid for r in remaining)


# ─── GET /api/reports/ ────────────────────────────────────────────────
async def test_get_reports_returns_list(client):
    resp = await client.get("/api/reports/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ─── POST /api/reports/ ───────────────────────────────────────────────
async def test_create_report(client):
    rc = await client.post("/api/receipts/", json={
        "date": "2026-05-14", "org": "Лента", "amount": 999.0})
    rid = rc.json()["id"]

    resp = await client.post("/api/reports/", json={
        "title": "Майский отчёт", "total": 999.0, "receiptIds": [rid]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] > 0
    assert body["title"] == "Майский отчёт"
    assert body["receiptIds"] == [rid]


# ─── PATCH /api/reports/{id} ──────────────────────────────────────────
async def test_patch_report_status(client, seeded):
    resp = await client.patch("/api/reports/1", json={"status": "Отправлено"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "Отправлено"


# ─── GET /api/cards/ ──────────────────────────────────────────────────
async def test_get_cards_returns_list(client, seeded):
    resp = await client.get("/api/cards/")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["name"] == "Корп.карта"


# ─── POST /api/cards/ ─────────────────────────────────────────────────
async def test_create_card(client):
    resp = await client.post("/api/cards/", json={"name": "Личная Сбер"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] > 0
    assert body["name"] == "Личная Сбер"


# ─── DELETE /api/cards/{id} ───────────────────────────────────────────
async def test_delete_card(client):
    created = await client.post("/api/cards/", json={"name": "Временная"})
    cid = created.json()["id"]

    resp = await client.delete(f"/api/cards/{cid}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    remaining = (await client.get("/api/cards/")).json()
    assert all(c["id"] != cid for c in remaining)


# ─── GET /api/receipts/suggest-payment ────────────────────────────────
async def test_suggest_payment_returns_card(client, seeded):
    resp = await client.get("/api/receipts/suggest-payment", params={"org": "Лукойл"})
    assert resp.status_code == 200
    assert resp.json()["payment"] == "Корп.карта"


async def test_suggest_payment_returns_null_when_no_history(client):
    resp = await client.get("/api/receipts/suggest-payment", params={"org": "НеизвестнаяОрг"})
    assert resp.status_code == 200
    assert resp.json()["payment"] is None
