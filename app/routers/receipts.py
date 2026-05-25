import base64
import binascii
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from app.auth import get_current_user
from app.categorization import auto_categorize
from app.database import get_pool

router = APIRouter(prefix="/api/receipts", tags=["receipts"])

# Enumerable values for `source` — the channel a receipt arrived via.
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
async def get_receipts(user: dict = Depends(get_current_user)):
    p = await get_pool()
    rows = await p.fetch("SELECT * FROM receipts WHERE org_id=$1 ORDER BY date DESC", user["org_id"])
    return [dict(r) for r in rows]

@router.get("/suggest-payment")
async def suggest_payment(org: str, user: dict = Depends(get_current_user)):
    p = await get_pool()
    row = await p.fetchrow("""
        SELECT payment FROM receipts
        WHERE org=$1 AND org_id=$2 AND payment IS NOT NULL AND payment <> 'Не указано'
        GROUP BY payment ORDER BY COUNT(*) DESC LIMIT 1
    """, org, user["org_id"])
    return {"payment": row["payment"] if row else None}

@router.get("/{id}/photo")
async def get_receipt_photo(id: int, user: dict = Depends(get_current_user)):
    """Return the receipt's photo (photo_url redirect, or inline raw_data.photo_base64)."""
    p = await get_pool()
    row = await p.fetchrow("SELECT photo_url, raw_data FROM receipts WHERE id=$1 AND org_id=$2", id, user["org_id"])
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
async def get_receipt(id: int, user: dict = Depends(get_current_user)):
    p = await get_pool()
    row = await p.fetchrow("SELECT * FROM receipts WHERE id=$1 AND org_id=$2", id, user["org_id"])
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)

@router.post("/")
async def create_receipt(r: ReceiptIn, user: dict = Depends(get_current_user)):
    p = await get_pool()
    source = r.source or DEFAULT_SOURCE

    # Для source='photo_ocr' fn ненадёжен (OCR может ошибиться в одной цифре
    # или сгаллюцинировать). photo_ocr всегда идёт в composite-ветку, даже
    # если OCR извлёк fn. OCR-извлечённый fn сохраняется в БД для справки,
    # но не используется для дедупа. Надёжный fn (qr_scan / fns / введённый
    # осознанно в manual) дедупится по fiscal number — бессрочно.
    if r.fn and source != "photo_ocr":
        existing = await p.fetchrow("SELECT id FROM receipts WHERE fn=$1 AND org_id=$2", r.fn, user["org_id"])
        if existing:
            raise HTTPException(status_code=409, detail={"error": "duplicate", "existing_id": existing["id"]})

    category = r.category
    if not category or category == "Не указано":
        category = auto_categorize(r.org)

    # Composite-ветка: чеки без надёжного fn — fn-less (manual без fn) ЛИБО
    # photo_ocr (его fn игнорируем). Матчит ВСЕ чеки с теми же composite-ключами
    # за последние 5 минут, независимо от наличия fn (поэтому здесь больше нет
    # 'AND fn IS NULL'). Ловит и двойной тап «Использовать» (дубль id 39/41, 0.19s
    # apart, source=photo_ocr), и случай «qr_scan создан с верным fn → тот же чек
    # приходит через photo_ocr с OCR-ошибкой в fn».
    # IS NOT DISTINCT FROM — чтобы NULL employee/payment/category матчили NULL
    # (у дубля 39/41 employee=NULL, plain '=' его бы не поймал). Сравниваем с
    # *итоговой* category — той, что реально уйдёт в БД. Риск ложных срабатываний
    # мал: composite-ключ включает employee/payment/category.
    if not r.fn or source == "photo_ocr":
        recent = await p.fetchrow(
            """SELECT id FROM receipts
               WHERE org_id = $1
                 AND date = $2
                 AND amount = $3
                 AND employee IS NOT DISTINCT FROM $4
                 AND payment  IS NOT DISTINCT FROM $5
                 AND category IS NOT DISTINCT FROM $6
                 AND created_at > NOW() - INTERVAL '5 minutes'
               LIMIT 1""",
            user["org_id"], r.date, r.amount, r.employee, r.payment, category,
        )
        if recent:
            raise HTTPException(status_code=409, detail={
                "error": "duplicate",
                "message": "Похожий чек уже добавлен",
                "existing_id": recent["id"],
            })

    row = await p.fetchrow(
        "INSERT INTO receipts (date,org,category,payment,amount,employee,fn,raw_data,source,photo_url,org_id) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *",
        r.date, r.org, category, r.payment, r.amount, r.employee, r.fn, r.raw_data,
        source, r.photo_url, user["org_id"],
    )
    return dict(row)

@router.post("/dedupe-cleanup/")
async def dedupe_cleanup(user: dict = Depends(get_current_user)):
    p = await get_pool()
    rows = await p.fetch("""
        SELECT MIN(id) AS keep_id, date, amount, org, COUNT(*) AS cnt
        FROM receipts WHERE org_id=$1
        GROUP BY date, amount, org
        HAVING COUNT(*) > 1
    """, user["org_id"])
    deleted_total = 0
    kept_total = 0
    for row in rows:
        await p.execute(
            "DELETE FROM receipts WHERE date=$1 AND amount=$2 AND org=$3 AND org_id=$4 AND id <> $5",
            row["date"], row["amount"], row["org"], user["org_id"], row["keep_id"]
        )
        deleted_total += row["cnt"] - 1
        kept_total += 1

    return {"deleted": deleted_total, "kept": kept_total}

class ReceiptPatch(BaseModel):
    category: Optional[str] = None
    payment: Optional[str] = None
    org: Optional[str] = None

@router.patch("/{id}")
async def patch_receipt(id: int, r: ReceiptPatch, user: dict = Depends(get_current_user)):
    p = await get_pool()
    fields, values = [], []
    for k, v in [("category", r.category), ("payment", r.payment), ("org", r.org)]:
        if v is not None:
            values.append(v)
            fields.append(f"{k}=${len(values)}")
    if not fields:
        row = await p.fetchrow("SELECT * FROM receipts WHERE id=$1 AND org_id=$2", id, user["org_id"])
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        return dict(row)
    values.append(id)
    values.append(user["org_id"])
    row = await p.fetchrow(
        f"UPDATE receipts SET {', '.join(fields)} "
        f"WHERE id=${len(values) - 1} AND org_id=${len(values)} RETURNING *",
        *values
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)

@router.delete("/{id}")
async def delete_receipt(id: int, user: dict = Depends(get_current_user)):
    p = await get_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM report_items WHERE receipt_id=$1", id)
            await conn.execute("DELETE FROM receipts WHERE id=$1 AND org_id=$2", id, user["org_id"])
    return {"ok": True}
