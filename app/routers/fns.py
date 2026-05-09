import os
import logging
import traceback
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/fns", tags=["fns"])

class CheckRequest(BaseModel):
    qr_raw: str

@router.post("/check")
async def check_receipt(req: CheckRequest):
    token = os.getenv("PROVERKACHEKA_TOKEN", "")
    logger.info("FNS check: token present=%s", bool(token))
    if not token:
        logger.info("FNS check: aborting — PROVERKACHEKA_TOKEN not set")
        raise HTTPException(status_code=503, detail="PROVERKACHEKA_TOKEN not set")

    target_url = "https://proverkacheka.com/api/v1/check/get"
    logger.info("FNS check: POST %s  qr_raw=%s", target_url, req.qr_raw[:80])

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                target_url,
                json={"token": token, "qrraw": req.qr_raw},
            )
            logger.info("FNS check: response status=%s", resp.status_code)
            logger.info("FNS check: response body (first 500)=%s", resp.text[:500])
            data = resp.json()
        except Exception:
            logger.info("FNS check: exception during request:\n%s", traceback.format_exc())
            raise HTTPException(status_code=502, detail=traceback.format_exc())

    if data.get("code") != 1:
        logger.info("FNS check: code != 1, data=%s", str(data)[:500])
        raise HTTPException(status_code=404, detail="Receipt not found in FNS")

    j = data.get("data", {}).get("json", {})
    return {
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
    }
