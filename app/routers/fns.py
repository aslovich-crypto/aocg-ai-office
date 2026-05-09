import os
import traceback
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/fns", tags=["fns"])

class CheckRequest(BaseModel):
    qr_raw: str

@router.post("/check")
async def check_receipt(req: CheckRequest):
    token = os.getenv("PROVERKACHEKA_TOKEN", "")
    print(f"[FNS] token present={bool(token)}", flush=True)
    if not token:
        print("[FNS] aborting — PROVERKACHEKA_TOKEN not set", flush=True)
        raise HTTPException(status_code=503, detail="PROVERKACHEKA_TOKEN not set")

    target_url = "https://proverkacheka.com/api/v1/check/get"
    print(f"[FNS] POST {target_url}  qr_raw={req.qr_raw[:80]}", flush=True)

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                target_url,
                json={"token": token, "qrraw": req.qr_raw},
            )
            print(f"[FNS] response status={resp.status_code}", flush=True)
            print(f"[FNS] response body={resp.text[:500]}", flush=True)
            data = resp.json()
        except Exception:
            tb = traceback.format_exc()
            print(f"[FNS] exception:\n{tb}", flush=True)
            raise HTTPException(status_code=502, detail=tb)

    if data.get("code") != 1:
        print(f"[FNS] code != 1, full response={str(data)[:500]}", flush=True)
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
