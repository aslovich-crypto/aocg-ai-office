import os
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/fns", tags=["fns"])

class CheckRequest(BaseModel):
    qr_raw: str

@router.post("/check")
async def check_receipt(req: CheckRequest):
    token = os.getenv("PROVERKACHEKA_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="PROVERKACHEKA_TOKEN not set")

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                "https://proverkacheka.com/api/v1/check/get",
                json={"token": token, "qrraw": req.qr_raw},
            )
            data = resp.json()
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

    if data.get("code") != 1:
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
