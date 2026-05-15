import asyncio
import os
import httpx
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/fns", tags=["fns"])

PROVERKACHEKA_URL = "https://proverkacheka.com/api/v1/check/get"
REQUEST_TIMEOUT = httpx.Timeout(10.0)
RETRY_DELAY = 2.0
PARTIAL_RESPONSE = {"status": "partial", "org": None, "items": None}


class CheckRequest(BaseModel):
    qr_raw: str


async def _fetch_check(client: httpx.AsyncClient, token: str, qr_raw: str) -> dict:
    """Single attempt; returns parsed json or raises httpx exception."""
    resp = await client.post(PROVERKACHEKA_URL, json={"token": token, "qrraw": qr_raw})
    resp.raise_for_status()
    return resp.json()


@router.post("/check")
async def check_receipt(req: CheckRequest):
    """
    Proxy QR string to proverkacheka.com.

    Resilience contract:
      - 10s timeout on each HTTP call.
      - One retry after RETRY_DELAY on timeout / network / bad-status / code != 1.
      - On total failure return 200 with {"status": "partial", ...} so the client
        can keep the manual flow alive instead of treating the receipt as broken.
      - On success include {"status": "ok", ...} explicitly so the client can
        branch without sniffing fields.
    """
    token = os.getenv("PROVERKACHEKA_TOKEN", "")
    if not token:
        print("[FNS] PROVERKACHEKA_TOKEN not set — returning partial", flush=True)
        return PARTIAL_RESPONSE

    print(f"[FNS] POST {PROVERKACHEKA_URL}  qr_raw={req.qr_raw[:80]}", flush=True)

    data: dict | None = None
    last_error: str | None = None
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for attempt in (1, 2):
            try:
                data = await _fetch_check(client, token, req.qr_raw)
                if data.get("code") == 1:
                    break
                last_error = f"proverkacheka code={data.get('code')}"
                print(f"[FNS] attempt {attempt}: {last_error}, body={str(data)[:300]}", flush=True)
                data = None
            except (httpx.TimeoutException, httpx.HTTPError) as e:
                last_error = f"{type(e).__name__}: {e}"
                print(f"[FNS] attempt {attempt}: {last_error}", flush=True)
                data = None
            if attempt == 1:
                await asyncio.sleep(RETRY_DELAY)

    if data is None:
        print(f"[FNS] both attempts failed ({last_error}) — returning partial", flush=True)
        return PARTIAL_RESPONSE

    j = data.get("data", {}).get("json", {})
    return {
        "status":  "ok",
        "org":     j.get("user", ""),
        "inn":     j.get("userInn", ""),
        "address": j.get("retailPlaceAddress", ""),
        "total":   j.get("totalSum", 0) / 100,
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
