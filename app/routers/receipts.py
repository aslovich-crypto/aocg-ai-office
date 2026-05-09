from fastapi import APIRouter
from app.database import get_pool
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

@router.get("/")
async def get_receipts():
    p = await get_pool()
    rows = await p.fetch("SELECT * FROM receipts ORDER BY date DESC")
    return [dict(r) for r in rows]

@router.post("/")
async def create_receipt(r: ReceiptIn):
    p = await get_pool()
    row = await p.fetchrow(
        "INSERT INTO receipts (date,org,category,payment,amount,employee) VALUES ($1,$2,$3,$4,$5,$6) RETURNING *",
        r.date, r.org, r.category, r.payment, r.amount, r.employee
    )
    return dict(row)

@router.delete("/{id}")
async def delete_receipt(id: int):
    p = await get_pool()
    await p.execute("DELETE FROM receipts WHERE id=$1", id)
    return {"ok": True}
