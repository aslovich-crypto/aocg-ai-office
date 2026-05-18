import base64
import binascii
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from app.categorization import auto_categorize
from app.database import get_pool

router = APIRouter(prefix="/api/receipts", tags=["receipts"])

# Enumerable values for `source` — the channel a receipt arrived via.
# We don't enforce at the DB level (TEXT column) but keep this list as the
# authoritative reference for the frontend and analytics:
#   manual    — user typed it in
#   qr_scan   — scanned a fiscal QR; FNS lookup succeeded
#   photo_ocr — OCR'd a photo via Claude Vision
#   fns       — created from FNS data through some other flow (reserved)
DEFAULT_SOURCE = "manual"


class ReceiptIn(BaseModel):
    date: date
    org: str
    category: Optional[str] = None
    payment: Optional[str] = None
    amount: float
    employee: Optional[str] = None
    fn: Optional[str] = None
    raw_data: Optional[dict] = None
    source: Optional[str] = None       # 'manual' | 'qr_scan' | 'photo_ocr' | 'fns'
    photo_url: Optional[str] = None    # external URL (Cloudflare R2 etc.) when set

@router.get("/")
async def get_receipts():
    p = await get_pool()
    rows = await p.fetch("SELECT * FROM receipts ORDER BY date DESC")
    return [dict(r) for r in rows]

@router.get("/suggest-payment")
async def suggest_payment(org: str):
    p = await get_pool()
    row = await p.fetchrow("""
        SELECT payment FROM receipts
        WHERE org=$1 AND payment IS NOT NULL AND payment <> 'Не указано'
        GROUP BY payment ORDER BY COUNT(*) DESC LIMIT 1
    """, org)
    return {"payment": row["payment"] if row else None}

@router.get("/{id}/photo")
async def get_receipt_photo(id: int):
    """
    Return the receipt's photo.

    Resolution order:
      1. photo_url  → 302 redirect (external storage; e.g. Cloudflare R2).
      2. raw_data.photo_base64 → inline image bytes (temporary, before R2).
      3. neither  → 404.

    Until R2 is wired up, /api/receipts/ocr/ stuffs the source photo into
    raw_data.photo_base64 and this endpoint serves it back as image/jpeg
    (browsers content-sniff PNG/WEBP fine too).
    """
    p = await get_pool()
    row = await p.fetchrow("SELECT photo_url, raw_data FROM receipts WHERE id=$1", id)
    if not row:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if row["photo_url"]:
        return RedirectResponse(url=row["photo_url"], status_code=302)
    raw = row["raw_data"] if isinstance(row["raw_data"], dict) else {}
    photo_b64 = raw.get("photo_base64") if raw else None
    if not photo_b64:
        raise HTTPException(status_code=404, detail="No photo for this receipt")
    try:
        photo_bytes = base64.b64decode(photo_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=500, detail="Corrupt photo data")
    return Response(content=photo_bytes, media_type="image/jpeg")

@router.get("/{id}")
async def get_receipt(id: int):
    p = await get_pool()
    row = await p.fetchrow("SELECT * FROM receipts WHERE id=$1", id)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)

@router.post("/")
async def create_receipt(r: ReceiptIn):
    p = await get_pool()

    if r.fn:
        existing = await p.fetchrow("SELECT id FROM receipts WHERE fn=$1", r.fn)
        if existing:
            raise HTTPException(status_code=409, detail={"error": "duplicate", "existing_id": existing["id"]})

    category = r.category
    if not category or category == "Не указано":
        category = auto_categorize(r.org)

    source = r.source or DEFAULT_SOURCE

    row = await p.fetchrow(
        "INSERT INTO receipts (date,org,category,payment,amount,employee,fn,raw_data,source,photo_url) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *",
        r.date, r.org, category, r.payment, r.amount, r.employee, r.fn, r.raw_data,
        source, r.photo_url,
    )
    return dict(row)

@router.post("/dedupe-cleanup/")
async def dedupe_cleanup():
    p = await get_pool()
    rows = await p.fetch("""
        SELECT MIN(id) AS keep_id, date, amount, org, COUNT(*) AS cnt
        FROM receipts
        GROUP BY date, amount, org
        HAVING COUNT(*) > 1
    """)
    deleted_total = 0
    kept_total = 0
    for row in rows:
        await p.execute(
            "DELETE FROM receipts WHERE date=$1 AND amount=$2 AND org=$3 AND id <> $4",
            row["date"], row["amount"], row["org"], row["keep_id"]
        )
        deleted_total += row["cnt"] - 1
        kept_total += 1

    return {"deleted": deleted_total, "kept": kept_total}

class ReceiptPatch(BaseModel):
    category: Optional[str] = None
    payment: Optional[str] = None
    org: Optional[str] = None

@router.patch("/{id}")
async def patch_receipt(id: int, r: ReceiptPatch):
    p = await get_pool()
    fields, values = [], []
    for k, v in [("category", r.category), ("payment", r.payment), ("org", r.org)]:
        if v is not None:
            values.append(v)
            fields.append(f"{k}=${len(values)}")
    if not fields:
        row = await p.fetchrow("SELECT * FROM receipts WHERE id=$1", id)
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        return dict(row)
    values.append(id)
    row = await p.fetchrow(
        f"UPDATE receipts SET {', '.join(fields)} WHERE id=${len(values)} RETURNING *",
        *values
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)

@router.delete("/{id}")
async def delete_receipt(id: int):
    p = await get_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM report_items WHERE receipt_id=$1", id)
            await conn.execute("DELETE FROM receipts WHERE id=$1", id)
    return {"ok": True}
