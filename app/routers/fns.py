import asyncio
import os
import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.categorization import categorize
from aocg_security.masking import mask_log_dict

router = APIRouter(prefix="/api/fns", tags=["fns"])

PROVERKACHEKA_URL = "https://proverkacheka.com/api/v1/check/get"
REQUEST_TIMEOUT = httpx.Timeout(10.0)
RETRY_DELAY = 2.0


class CheckRequest(BaseModel):
    qr_raw: str


async def _fetch_check(client: httpx.AsyncClient, token: str, qr_raw: str) -> dict:
    """Single attempt; returns parsed json or raises an httpx exception."""
    resp = await client.post(PROVERKACHEKA_URL, json={"token": token, "qrraw": qr_raw})
    resp.raise_for_status()
    return resp.json()


@router.post("/check")
async def check_receipt(req: CheckRequest):
    """
    Proxy a QR string to proverkacheka.com and map the outcome to a distinct
    HTTP status so the client can branch precisely:

      200 ok              — receipt found (proverkacheka code == 1)
      404 not_found       — proverkacheka answered but didn't confirm the
                            receipt (any code != 1 from a 2xx response)
      503 fns_unavailable — timeout / network / non-2xx, after one retry
      400/500 error       — our side (empty qr_raw / missing token)

    proverkacheka exposes only `code` (1 = success) and does NOT separate
    "not found" from other failures, so we use a heuristic (documented per the
    spec): a parsed HTTP response with code != 1 ⇒ not_found; any transport
    failure (timeout / connect error / non-2xx) ⇒ unavailable. fn/date are
    parsed from the QR on the client, so they are not echoed here.

    Edge case (accepted, not a bug): a malformed QR that isn't a fiscal receipt
    code also comes back as 404 not_found with the "not registered" message —
    proverkacheka simply doesn't confirm it, which is indistinguishable here
    from a genuine unregistered receipt.
    """
    if not req.qr_raw or not req.qr_raw.strip():
        return JSONResponse(status_code=400, content={"status": "error", "message": "qr_raw is empty"})

    token = os.getenv("PROVERKACHEKA_TOKEN", "")
    if not token:
        print("[FNS] PROVERKACHEKA_TOKEN not set", flush=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": "PROVERKACHEKA_TOKEN not set"})

    # 152-ФЗ: не логируем содержимое QR (там ФН/сумма/ФПД) — только длину.
    print(f"[FNS] POST {PROVERKACHEKA_URL}  qr_raw len={len(req.qr_raw)}", flush=True)

    data: dict | None = None
    last_error: str | None = None
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for attempt in (1, 2):
            try:
                data = await _fetch_check(client, token, req.qr_raw)
                break  # got an HTTP response (any code) — retry only on transport failure
            except (httpx.TimeoutException, httpx.HTTPError) as e:
                last_error = f"{type(e).__name__}: {e}"
                print(f"[FNS] attempt {attempt}: {last_error}", flush=True)
                data = None
            if attempt == 1:
                await asyncio.sleep(RETRY_DELAY)

    # C — transport failure after retry: ФНС недоступна.
    if data is None:
        print(f"[FNS] unavailable ({last_error})", flush=True)
        return JSONResponse(status_code=503, content={
            "status": "fns_unavailable",
            "message": "Сервис проверки ФНС временно недоступен",
        })

    # B — proverkacheka responded but the receipt isn't confirmed/registered.
    if data.get("code") != 1:
        print(f"[FNS] not_found: code={data.get('code')} body={str(mask_log_dict(data))[:200]}", flush=True)
        return JSONResponse(status_code=404, content={
            "status": "not_found",
            "message": "Чек не зарегистрирован в ФНС. Возможно, ему больше 30 дней или он не был передан оператору.",
        })

    # A — success.
    j = data.get("data", {}).get("json", {})
    org = j.get("user", "") or ""
    return {
        "status":   "ok",
        "org":      org,
        "category": categorize(org, j.get("items") or [], brand=j.get("retailPlace")),
        "inn":      j.get("userInn", ""),
        "address":  j.get("retailPlaceAddress", ""),
        "total":    j.get("totalSum", 0) / 100,
        "items": [
            {
                "name":     item.get("name", ""),
                "quantity": item.get("quantity", 1),
                "price":    item.get("price", 0) / 100,
                "sum":      item.get("sum", 0) / 100,
            }
            for item in j.get("items", [])
        ],
        "raw": j,
    }
