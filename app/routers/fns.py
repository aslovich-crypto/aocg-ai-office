import asyncio
import os
import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import get_current_user
from app.categorization import categorize
from aocg_security.masking import mask_log_dict

router = APIRouter(prefix="/api/fns", tags=["fns"])

PROVERKACHEKA_URL = "https://proverkacheka.com/api/v1/check/get"
REQUEST_TIMEOUT = httpx.Timeout(10.0)

# ⚠️ КОДЫ, КОТОРЫМИ СЕРВИС ОТКАЗЫВАЕТ НАМ, А НЕ ЧЕКУ. Замер 31.08.2026:
# с негодным ключом proverkacheka отвечает HTTP 200 и телом
# {"code":401,"data":"Не авторизован (не представился)…"}.
КОДЫ_ОТКАЗА_НАМ = {401, 402, 403, 429}
RETRY_DELAY = 2.0


class CheckRequest(BaseModel):
    qr_raw: str


async def _fetch_check(client: httpx.AsyncClient, token: str, qr_raw: str) -> dict:
    """Single attempt; returns parsed json or raises an httpx exception."""
    resp = await client.post(PROVERKACHEKA_URL, json={"token": token, "qrraw": qr_raw})
    resp.raise_for_status()
    return resp.json()


# ⚠️ ОДНА ДВЕРЬ ДЛЯ ВСЕХ, КТО СПРАШИВАЕТ ФНС (T132, 31.08.2026). Ручку
# дозапроса по сохранённому чеку (`receipts.py`) писать со своим разбором
# ответа значило бы завести ВТОРУЮ дверь с той же логикой — и однажды
# починить одну из двух. Здесь принимается решение по ответу; кто спросил,
# роли не играет.
#
# Возвращает (http-код, тело). Код 200 — данные разобраны и лежат в теле.
async def спросить_фнс(qr_raw: str) -> tuple[int, dict]:
    token = os.getenv("PROVERKACHEKA_TOKEN", "")
    if not token:
        print("[FNS] PROVERKACHEKA_TOKEN not set", flush=True)
        return 500, {"status": "error", "message": "PROVERKACHEKA_TOKEN not set"}

    # 152-ФЗ: не логируем содержимое QR (там ФН/сумма/ФПД) — только длину.
    print(f"[FNS] POST {PROVERKACHEKA_URL}  qr_raw len={len(qr_raw)}", flush=True)

    data: dict | None = None
    last_error: str | None = None
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for attempt in (1, 2):
            try:
                data = await _fetch_check(client, token, qr_raw)
                break  # получен HTTP-ответ (любой код) — повтор только на транспорте
            except (httpx.TimeoutException, httpx.HTTPError) as e:
                last_error = f"{type(e).__name__}: {e}"
                print(f"[FNS] attempt {attempt}: {last_error}", flush=True)
                data = None
            if attempt == 1:
                await asyncio.sleep(RETRY_DELAY)

    if data is None:
        print(f"[FNS] unavailable ({last_error})", flush=True)
        return 503, {
            "status": "fns_unavailable",
            "message": "Сервис проверки ФНС временно недоступен",
        }

    код = data.get("code")
    if код in КОДЫ_ОТКАЗА_НАМ:
        подпись = str(data.get("data") or "")[:200]
        print(f"[FNS] СЕРВИС ОТКАЗАЛ НАМ: code={код} {подпись}", flush=True)
        return 502, {
            "status": "fns_rejected_us",
            "message": (
                f"Сервис проверки чеков не принял наш доступ (код {код}). "
                "Чек здесь ни при чём — проверьте ключ и остаток запросов."
            ),
        }

    if код != 1:
        print(
            f"[FNS] not_found: code={код} body={str(mask_log_dict(data))[:200]}",
            flush=True,
        )
        return 404, {
            "status": "not_found",
            "message": (
                "Налоговая пока не отдаёт данные по этому чеку. Так бывает: "
                "обычно они появляются в течение суток."
            ),
        }

    j = data.get("data", {}).get("json", {})
    org = j.get("user", "") or ""
    return 200, {
        "status": "ok",
        "org": org,
        "category": categorize(org, j.get("items") or [], brand=j.get("retailPlace")),
        "inn": j.get("userInn", ""),
        "address": j.get("retailPlaceAddress", ""),
        "total": j.get("totalSum", 0) / 100,
        "items": [
            {
                "name": item.get("name", ""),
                "quantity": item.get("quantity", 1),
                "price": item.get("price", 0) / 100,
                "sum": item.get("sum", 0) / 100,
            }
            for item in j.get("items", [])
        ],
        "raw": j,
    }


@router.post("/check")
async def check_receipt(req: CheckRequest, user: dict = Depends(get_current_user)):
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
        return JSONResponse(
            status_code=400, content={"status": "error", "message": "qr_raw is empty"}
        )

    # ⚠️ ВСЯ ЛОГИКА — В `спросить_фнс`. Здесь только приведение к HTTP: ручка
    # дозапроса по сохранённому чеку ходит тем же путём, и две копии разбора
    # ответа однажды разошлись бы (одну починили, вторую забыли).
    код, тело = await спросить_фнс(req.qr_raw)
    if код == 200:
        return тело
    return JSONResponse(status_code=код, content=тело)
