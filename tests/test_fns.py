"""Tests for POST /api/fns/check — the four distinct outcomes.

proverkacheka.com is never hit: _fetch_check is monkeypatched per test, and
RETRY_DELAY is zeroed so the timeout case doesn't actually sleep.
"""

import httpx
import pytest

import app.routers.fns as fns


@pytest.fixture
def fns_env(monkeypatch):
    monkeypatch.setenv("PROVERKACHEKA_TOKEN", "test-token")
    monkeypatch.setattr(fns, "RETRY_DELAY", 0)  # no 2s sleep between retries


async def test_fns_check_returns_200_with_ok_when_receipt_found(client, fns_env, monkeypatch):
    async def fake(_client, _token, _qr):
        return {"code": 1, "data": {"json": {
            "user": "ООО Ромашка", "userInn": "7700000000",
            "retailPlaceAddress": "Москва", "totalSum": 123400,
            "items": [{"name": "Кофе", "quantity": 1, "price": 123400, "sum": 123400}],
        }}}
    monkeypatch.setattr(fns, "_fetch_check", fake)

    resp = await client.post("/api/fns/check", json={"qr_raw": "t=20260101T1200&s=1234.00&fn=1&i=1&fp=1&n=1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["org"] == "ООО Ромашка"
    assert body["total"] == 1234.0
    assert body["items"][0]["name"] == "Кофе"


async def test_fns_check_returns_404_when_receipt_not_found(client, fns_env, monkeypatch):
    # proverkacheka answered (HTTP 2xx) but code != 1 → receipt not confirmed.
    async def fake(_client, _token, _qr):
        return {"code": 0, "data": "Чек не найден"}
    monkeypatch.setattr(fns, "_fetch_check", fake)

    resp = await client.post("/api/fns/check", json={"qr_raw": "t=20200101T1200&s=1.00&fn=1&i=1&fp=1&n=1"})
    assert resp.status_code == 404
    assert resp.json()["status"] == "not_found"


async def test_fns_check_returns_503_when_proverkacheka_timeout(client, fns_env, monkeypatch):
    # Transport failure on both attempts → ФНС недоступна.
    async def fake(_client, _token, _qr):
        raise httpx.TimeoutException("read timed out")
    monkeypatch.setattr(fns, "_fetch_check", fake)

    resp = await client.post("/api/fns/check", json={"qr_raw": "t=20260101T1200&s=1.00&fn=1&i=1&fp=1&n=1"})
    assert resp.status_code == 503
    assert resp.json()["status"] == "fns_unavailable"


async def test_fns_check_returns_500_when_token_missing(client, monkeypatch):
    monkeypatch.delenv("PROVERKACHEKA_TOKEN", raising=False)

    resp = await client.post("/api/fns/check", json={"qr_raw": "t=20260101T1200&s=1.00&fn=1&i=1&fp=1&n=1"})
    assert resp.status_code == 500
    assert resp.json()["status"] == "error"
