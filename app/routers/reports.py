from fastapi import APIRouter
from app.database import get_pool
from pydantic import BaseModel
from typing import List

router = APIRouter(prefix="/api/reports", tags=["reports"])

class ReportIn(BaseModel):
    title: str
    total: float
    receiptIds: List[int]

class StatusIn(BaseModel):
    status: str

@router.get("/")
async def get_reports():
    p = await get_pool()
    reports = await p.fetch("SELECT * FROM reports ORDER BY created DESC")
    items = await p.fetch("SELECT * FROM report_items")
    result = []
    for rep in reports:
        d = dict(rep)
        d["receiptIds"] = [i["receipt_id"] for i in items if i["report_id"] == rep["id"]]
        result.append(d)
    return result

@router.post("/")
async def create_report(r: ReportIn):
    p = await get_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            rep = await conn.fetchrow("INSERT INTO reports (title,total) VALUES ($1,$2) RETURNING *", r.title, r.total)
            for rid in r.receiptIds:
                await conn.execute("INSERT INTO report_items VALUES ($1,$2)", rep["id"], rid)
    d = dict(rep)
    d["receiptIds"] = r.receiptIds
    return d

@router.patch("/{id}")
async def update_status(id: int, s: StatusIn):
    p = await get_pool()
    row = await p.fetchrow("UPDATE reports SET status=$1 WHERE id=$2 RETURNING *", s.status, id)
    return dict(row)
