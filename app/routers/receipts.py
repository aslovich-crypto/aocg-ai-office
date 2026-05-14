from fastapi import APIRouter, HTTPException
from app.database import get_pool
from app.categorization import auto_categorize
from pydantic import BaseModel
from typing import Optional
from datetime import date

router = APIRouter(prefix="/api/receipts", tags=["receipts"])

class ReceiptIn(BaseModel):
    date: date
    org: str
    category: Optional[str] = None
    payment: Optional[str] = None
    amount: float
    employee: Optional[str] = None
    fn: Optional[str] = None
    raw_data: Optional[dict] = None

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

    row = await p.fetchrow(
        "INSERT INTO receipts (date,org,category,payment,amount,employee,fn,raw_data) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *",
        r.date, r.org, category, r.payment, r.amount, r.employee, r.fn, r.raw_data
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
    for i, (k, v) in enumerate([("category", r.category), ("payment", r.payment), ("org", r.org)], start=1):
        if v is not None:
            fields.append(f"{k}=${i}")
            values.append(v)
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
    await p.execute("DELETE FROM receipts WHERE id=$1", id)
    return {"ok": True}
