from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user
from app.database import get_pool

router = APIRouter(prefix="/api/reports", tags=["reports"])

class ReportIn(BaseModel):
    title: str
    total: float
    receiptIds: List[int]

class StatusIn(BaseModel):
    status: str

@router.get("/")
async def get_reports(user: dict = Depends(get_current_user)):
    p = await get_pool()
    reports = await p.fetch("SELECT * FROM reports WHERE org_id=$1 ORDER BY created DESC", user["org_id"])
    items = await p.fetch(
        "SELECT ri.* FROM report_items ri JOIN reports r ON r.id = ri.report_id WHERE r.org_id=$1",
        user["org_id"])
    result = []
    for rep in reports:
        d = dict(rep)
        d["receiptIds"] = [i["receipt_id"] for i in items if i["report_id"] == rep["id"]]
        result.append(d)
    return result

@router.post("/")
async def create_report(r: ReportIn, user: dict = Depends(get_current_user)):
    p = await get_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            rep = await conn.fetchrow(
                "INSERT INTO reports (title,total,org_id) VALUES ($1,$2,$3) RETURNING *",
                r.title, r.total, user["org_id"])
            # IDOR-защита: все receiptIds обязаны принадлежать организации
            # пользователя. Проверяем ОДНИМ запросом, ДО вставок и внутри
            # транзакции — любой чужой/несуществующий id → 403 и откат всей
            # вставки (для финпродукта явная ошибка лучше тихого пропуска).
            if r.receiptIds:
                owned = await conn.fetch(
                    "SELECT id FROM receipts WHERE id = ANY($1::int[]) AND org_id = $2",
                    r.receiptIds, user["org_id"])
                if {row["id"] for row in owned} != set(r.receiptIds):
                    # Обобщённый detail: НЕ перечисляем недоступные id, чтобы не
                    # подтверждать их существование (это тоже утечка).
                    raise HTTPException(status_code=403, detail="Один или несколько чеков недоступны")
                for rid in r.receiptIds:
                    await conn.execute("INSERT INTO report_items VALUES ($1,$2)", rep["id"], rid)
    d = dict(rep)
    d["receiptIds"] = r.receiptIds
    return d

@router.patch("/{id}")
async def update_status(id: int, s: StatusIn, user: dict = Depends(get_current_user)):
    p = await get_pool()
    row = await p.fetchrow(
        "UPDATE reports SET status=$1 WHERE id=$2 AND org_id=$3 RETURNING *",
        s.status, id, user["org_id"])
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)
