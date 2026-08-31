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


async def test_fns_check_returns_200_with_ok_when_receipt_found(
    client, fns_env, monkeypatch
):
    async def fake(_client, _token, _qr):
        return {
            "code": 1,
            "data": {
                "json": {
                    "user": "ООО Ромашка",
                    "userInn": "7700000000",
                    "retailPlaceAddress": "Москва",
                    "totalSum": 123400,
                    "items": [
                        {"name": "Кофе", "quantity": 1, "price": 123400, "sum": 123400}
                    ],
                }
            },
        }

    monkeypatch.setattr(fns, "_fetch_check", fake)

    resp = await client.post(
        "/api/fns/check", json={"qr_raw": "t=20260101T1200&s=1234.00&fn=1&i=1&fp=1&n=1"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["org"] == "ООО Ромашка"
    assert body["total"] == 1234.0
    assert body["items"][0]["name"] == "Кофе"


async def test_fns_check_returns_404_when_receipt_not_found(
    client, fns_env, monkeypatch
):
    # proverkacheka answered (HTTP 2xx) but code != 1 → receipt not confirmed.
    async def fake(_client, _token, _qr):
        return {"code": 0, "data": "Чек не найден"}

    monkeypatch.setattr(fns, "_fetch_check", fake)

    resp = await client.post(
        "/api/fns/check", json={"qr_raw": "t=20200101T1200&s=1.00&fn=1&i=1&fp=1&n=1"}
    )
    assert resp.status_code == 404
    assert resp.json()["status"] == "not_found"


async def test_отказ_нам_не_выдаётся_за_незарегистрированный_чек(
    client, fns_env, monkeypatch
):
    """⚠️ ЛОВИТ ЛОЖНУЮ ПРИЧИНУ, А НЕ ПОЛОМКУ ЗАПРОСА.

    Замер 31.08.2026: с негодным ключом proverkacheka отвечает HTTP 200 и
    телом {"code":401,"data":"Не авторизован…"}. Прежняя редакция сваливала
    любой code != 1 в «чек не зарегистрирован» — человек читал, что виноват
    его чек, и шёл проверять реквизиты, хотя виноват наш ключ. Тот же класс,
    что молчащий пропуск: прибор называет не ту причину.
    """
    for код in (401, 402, 403, 429):

        async def fake(_client, _token, _qr, _к=код):
            return {"code": _к, "data": "Не авторизован (не представился)."}

        monkeypatch.setattr(fns, "_fetch_check", fake)
        resp = await client.post(
            "/api/fns/check",
            json={"qr_raw": "t=20200101T1200&s=1.00&fn=1&i=1&fp=1&n=1"},
        )
        assert resp.status_code == 502, f"код {код} отдан как {resp.status_code}"
        тело = resp.json()
        assert тело["status"] == "fns_rejected_us"
        assert "Чек здесь ни при чём" in тело["message"]
        assert "не зарегистрирован" not in тело["message"]


async def test_настоящий_незарегистрированный_чек_остаётся_404(
    client, fns_env, monkeypatch
):
    """Обратная сторона: отделив отказ сервиса, нельзя потерять настоящий 404."""

    async def fake(_client, _token, _qr):
        return {"code": 0, "data": "Чек не найден"}

    monkeypatch.setattr(fns, "_fetch_check", fake)
    resp = await client.post(
        "/api/fns/check", json={"qr_raw": "t=20200101T1200&s=1.00&fn=1&i=1&fp=1&n=1"}
    )
    assert resp.status_code == 404
    assert resp.json()["status"] == "not_found"


async def test_fns_check_returns_503_when_proverkacheka_timeout(
    client, fns_env, monkeypatch
):
    # Transport failure on both attempts → ФНС недоступна.
    async def fake(_client, _token, _qr):
        raise httpx.TimeoutException("read timed out")

    monkeypatch.setattr(fns, "_fetch_check", fake)

    resp = await client.post(
        "/api/fns/check", json={"qr_raw": "t=20260101T1200&s=1.00&fn=1&i=1&fp=1&n=1"}
    )
    assert resp.status_code == 503
    assert resp.json()["status"] == "fns_unavailable"


async def test_fns_check_returns_500_when_token_missing(client, monkeypatch):
    monkeypatch.delenv("PROVERKACHEKA_TOKEN", raising=False)

    resp = await client.post(
        "/api/fns/check", json={"qr_raw": "t=20260101T1200&s=1.00&fn=1&i=1&fp=1&n=1"}
    )
    assert resp.status_code == 500
    assert resp.json()["status"] == "error"
