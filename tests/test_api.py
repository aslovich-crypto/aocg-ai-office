"""API endpoint tests for AOCG AI Office.

Run against an in-memory fake pool (see conftest.py) — no real database is
touched. Each test gets a fresh store via the `db` / `seeded` fixtures.
"""

from datetime import date, datetime, timedelta


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


# ─── Manual-receipt soft-dedup (no fn) -> 409 within 5 minutes ────────
async def test_manual_receipt_duplicate_within_5min_returns_409(client):
    payload = {"date": "2025-05-21", "org": "ООО Тепленькая пошла", "amount": 6400.0,
               "category": "Питание", "payment": "Корпоративная 3950", "source": "manual"}
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200

    # Identical manual receipt seconds later — the impatient double-tap case.
    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["error"] == "duplicate"
    assert detail["existing_id"] == first.json()["id"]


async def test_manual_receipt_same_data_after_5min_succeeds(client, db):
    # Same receipt, but the earlier one was created > 5 min ago: not a dup.
    old = datetime.utcnow() - timedelta(minutes=6)
    db.receipts.append(dict(id=1, date=date(2025, 5, 21), org="ООО Тепленькая пошла",
                            category="Питание", payment="Корпоративная 3950",
                            amount=6400.0, employee=None, fn=None, raw_data=None,
                            source="manual", photo_url=None, org_id=1, created_at=old))
    db._rid = 1

    payload = {"date": "2025-05-21", "org": "ООО Тепленькая пошла", "amount": 6400.0,
               "category": "Питание", "payment": "Корпоративная 3950", "source": "manual"}
    resp = await client.post("/api/receipts/", json=payload)
    assert resp.status_code == 200
    assert resp.json()["id"] != 1  # a brand-new receipt, not the stale one


async def test_qr_receipt_dedupe_still_works_by_fn(client):
    # QR/FNS path is untouched: dedup is by fiscal number, not the 5-min window.
    payload = {"date": "2025-05-21", "org": "Лукойл", "amount": 3000.0,
               "fn": "QR-FN-555", "source": "qr_scan"}
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200

    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 409
    assert second.json()["detail"]["existing_id"] == first.json()["id"]


# ─── Fn-less soft-dedup now covers every source, not just manual ──────
async def test_photo_ocr_duplicate_within_5min_returns_409(client):
    # The real prod dup (id 39/41) was source=photo_ocr, 0.19s apart, no fn.
    payload = {"date": "2026-05-21", "org": "ООО Теплопроводная пошла", "amount": 6400.0,
               "category": "Питание", "payment": "Корпоративная 3950", "source": "photo_ocr"}
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200

    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["error"] == "duplicate"
    assert detail["existing_id"] == first.json()["id"]


async def test_manual_and_photo_ocr_cross_dedupe(client):
    # Different sources, both fn-less, identical fields → dedup by fn IS NULL.
    base = {"date": "2026-05-21", "org": "Кафе Уют", "amount": 1500.0,
            "category": "Питание", "payment": "Наличные"}
    first = await client.post("/api/receipts/", json={**base, "source": "manual"})
    assert first.status_code == 200

    second = await client.post("/api/receipts/", json={**base, "source": "photo_ocr"})
    assert second.status_code == 409
    assert second.json()["detail"]["existing_id"] == first.json()["id"]


async def test_qr_with_fn_dedupe_still_by_fn(client):
    # Receipts WITH a fiscal number keep deduping by fn, never the 5-min window.
    payload = {"date": "2026-05-21", "org": "Лукойл", "amount": 3000.0,
               "fn": "QR-FN-777", "source": "qr_scan"}
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200

    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 409
    assert second.json()["detail"]["existing_id"] == first.json()["id"]


# ─── PATCH /api/receipts/{id} ─────────────────────────────────────────
async def test_patch_receipt_single_field(client, seeded):
    resp = await client.patch("/api/receipts/1", json={"payment": "Личная карта"})
    assert resp.status_code == 200
    assert resp.json()["payment"] == "Личная карта"
    assert resp.json()["org"] == "Лукойл"  # unchanged


async def test_patch_receipt_multiple_fields(client, seeded):
    resp = await client.patch("/api/receipts/1", json={
        "category": "Прочее", "org": "Газпром"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "Прочее"
    assert body["org"] == "Газпром"


async def test_patch_receipt_no_fields_returns_existing(client, seeded):
    resp = await client.patch("/api/receipts/1", json={})
    assert resp.status_code == 200
    assert resp.json()["org"] == "Лукойл"


async def test_patch_receipt_not_found(client):
    resp = await client.patch("/api/receipts/999", json={"category": "X"})
    assert resp.status_code == 404


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


# ─── POST /api/receipts/ocr/ ──────────────────────────────────────────
# A 1×1 PNG — anything we'd actually OCR is too big to inline, and the
# Anthropic client is mocked end-to-end so the image bytes never reach it.
import base64
import io

import pytest
from anthropic import APITimeoutError

import app.routers.ocr as ocr_module

_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class _Block:
    """Minimal stand-in for an Anthropic text content block."""
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeMessages:
    def __init__(self, behavior):
        self._behavior = behavior

    async def create(self, **kwargs):
        return self._behavior(kwargs)


class _FakeClient:
    """Stand-in for AsyncAnthropic.with_options(...) result."""
    def __init__(self, behavior):
        self.messages = _FakeMessages(behavior)


def _install_fake(monkeypatch, behavior):
    """Replace the module-level Anthropic client with one that runs `behavior`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class _Stub:
        def with_options(self, **_):
            return _FakeClient(behavior)

    monkeypatch.setattr(ocr_module, "_anthropic_client", _Stub())


async def test_ocr_rejects_non_image(client):
    files = {"file": ("note.txt", io.BytesIO(b"hello"), "text/plain")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 400
    assert "Unsupported" in resp.json()["detail"]


async def test_ocr_rejects_oversized_file(client):
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 10)
    files = {"file": ("big.png", io.BytesIO(big), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 400
    assert "exceeds" in resp.json()["detail"].lower()


async def test_ocr_rejects_empty_file(client):
    files = {"file": ("empty.png", io.BytesIO(b""), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 400


async def test_ocr_happy_path(client, monkeypatch):
    payload = {
        "org": "Магнит", "inn": "7707083893", "address": "Москва",
        "date": "2026-05-15", "time": "13:42", "amount": 1234.56,
        "items": [{"name": "Молоко", "qty": 1, "price": 89.0, "total": 89.0}],
        "nds": 123.45, "payment_type": "card", "fn": "FN-1",
        "confidence": "high",
    }
    import json as _json
    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["org"] == "Магнит"
    assert body["amount"] == 1234.56
    # auto-categorization picks up "Магнит" → "Продукты"
    assert body["category"] == "Продукты"


async def test_ocr_strips_markdown_fences(client, monkeypatch):
    """Claude sometimes wraps JSON in ```json ... ``` despite the prompt."""
    wrapped = '```json\n{"org": "Лукойл", "amount": 3000, "confidence": "medium"}\n```'
    _install_fake(monkeypatch, lambda kw: _Response(wrapped))

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["org"] == "Лукойл"
    assert body["category"] == "Топливо"


async def test_ocr_timeout_returns_low_confidence(client, monkeypatch):
    def boom(_kw):
        raise APITimeoutError(request=None)
    _install_fake(monkeypatch, boom)

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    # User said: timeout / unreadable -> low-confidence object, NOT 500.
    assert resp.status_code == 200
    body = resp.json()
    assert body["confidence"] == "low"
    assert body["amount"] is None
    assert body["org"] is None


async def test_ocr_garbage_response_returns_low_confidence(client, monkeypatch):
    _install_fake(monkeypatch, lambda kw: _Response("sorry, I cannot read this"))

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200
    assert resp.json()["confidence"] == "low"


async def test_ocr_missing_api_key_returns_low_confidence(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Don't install a fake — we shouldn't reach the client at all.
    monkeypatch.setattr(ocr_module, "_anthropic_client", None)

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200
    assert resp.json()["confidence"] == "low"


# ─── POST /api/consent/ ───────────────────────────────────────────────
async def test_post_consent_records_row(client, db):
    resp = await client.post("/api/consent/", json={"user_id": "local_user"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] > 0
    assert body["policy_version"] == "1.0"
    assert body["consent_at"] is not None
    # row landed in the store with the frozen text
    assert len(db.consents) == 1
    assert db.consents[0]["user_id"] == "local_user"
    assert "Шукалович" in db.consents[0]["consent_text"]


async def test_post_consent_with_ip(client, db):
    resp = await client.post("/api/consent/", json={"user_id": "u1", "ip_address": "203.0.113.4"})
    assert resp.status_code == 200
    assert db.consents[0]["ip_address"] == "203.0.113.4"


async def test_post_consent_appends_on_reagree(client, db):
    """Re-agreement is intentional — we append rather than upsert."""
    await client.post("/api/consent/", json={"user_id": "u1"})
    await client.post("/api/consent/", json={"user_id": "u1"})
    assert len(db.consents) == 2


# ─── GET /api/consent/{user_id} ───────────────────────────────────────
async def test_get_consent_returns_null_when_none(client):
    resp = await client.get("/api/consent/never_consented")
    assert resp.status_code == 200
    assert resp.json() is None


async def test_get_consent_returns_latest(client, db):
    await client.post("/api/consent/", json={"user_id": "u1"})
    second = await client.post("/api/consent/", json={"user_id": "u1"})
    resp = await client.get("/api/consent/u1")
    assert resp.status_code == 200
    body = resp.json()
    # 'latest' = highest id, which the POST returned
    assert body["id"] == second.json()["id"]
    assert body["policy_version"] == "1.0"


async def test_get_consent_isolates_users(client, db):
    await client.post("/api/consent/", json={"user_id": "alice"})
    resp = await client.get("/api/consent/bob")
    assert resp.status_code == 200
    assert resp.json() is None


# ─── POST /api/receipts/  source + photo_url ──────────────────────────
async def test_create_receipt_defaults_source_to_manual(client):
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0}
    body = (await client.post("/api/receipts/", json=payload)).json()
    assert body["source"] == "manual"
    assert body["photo_url"] is None


async def test_create_receipt_honors_explicit_source(client):
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0,
               "source": "qr_scan"}
    body = (await client.post("/api/receipts/", json=payload)).json()
    assert body["source"] == "qr_scan"


async def test_create_receipt_persists_photo_url(client):
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0,
               "source": "photo_ocr", "photo_url": "https://r2.example/abc.jpg"}
    body = (await client.post("/api/receipts/", json=payload)).json()
    assert body["source"] == "photo_ocr"
    assert body["photo_url"] == "https://r2.example/abc.jpg"


async def test_get_receipts_returns_source_field(client, seeded):
    body = (await client.get("/api/receipts/")).json()
    assert "source" in body[0]
    assert body[0]["source"] == "manual"  # seeded receipt defaults


# ─── GET /api/receipts/{id}/photo ─────────────────────────────────────
import base64 as _b64

# A minimal 1×1 PNG so the byte-equality assertion is meaningful.
_PNG_1x1_BYTES = _b64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


async def test_get_photo_404_when_receipt_missing(client):
    resp = await client.get("/api/receipts/9999/photo")
    assert resp.status_code == 404


async def test_get_photo_404_when_no_photo(client):
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0}
    created = (await client.post("/api/receipts/", json=payload)).json()
    resp = await client.get(f"/api/receipts/{created['id']}/photo")
    assert resp.status_code == 404


async def test_get_photo_returns_inline_bytes_from_base64(client):
    photo_b64 = _b64.b64encode(_PNG_1x1_BYTES).decode("ascii")
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0,
               "source": "photo_ocr",
               "raw_data": {"photo_base64": photo_b64, "items": []}}
    created = (await client.post("/api/receipts/", json=payload)).json()
    resp = await client.get(f"/api/receipts/{created['id']}/photo")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")
    assert resp.content == _PNG_1x1_BYTES


async def test_get_photo_redirects_when_photo_url_set(client):
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0,
               "source": "photo_ocr", "photo_url": "https://r2.example/abc.jpg"}
    created = (await client.post("/api/receipts/", json=payload)).json()
    resp = await client.get(f"/api/receipts/{created['id']}/photo",
                            follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://r2.example/abc.jpg"


async def test_get_photo_prefers_url_over_base64(client):
    """When both are present the external URL wins — R2 supersedes inline."""
    photo_b64 = _b64.b64encode(_PNG_1x1_BYTES).decode("ascii")
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0,
               "source": "photo_ocr",
               "photo_url": "https://r2.example/abc.jpg",
               "raw_data": {"photo_base64": photo_b64}}
    created = (await client.post("/api/receipts/", json=payload)).json()
    resp = await client.get(f"/api/receipts/{created['id']}/photo",
                            follow_redirects=False)
    assert resp.status_code == 302
