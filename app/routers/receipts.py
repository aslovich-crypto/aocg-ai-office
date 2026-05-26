import base64
import binascii
import logging
from datetime import date
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from app.auth import get_current_user
from app.categorization import auto_categorize
from app.database import get_pool
from app.parsers.fns_parser import parse_fns_response
from app.parsers.items_parser import parse_fns_items

logger = logging.getLogger(__name__)

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
    fn: Optional[str] = None            # deprecated — фронт пока шлёт сюда; переходный alias
    kkt_fn: Optional[str] = None        # фискальный номер ККТ (заменяет fn)
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
    effective_kkt_fn = r.kkt_fn or r.fn   # переходный fallback: фронт пока шлёт fn

    # Для source='photo_ocr' kkt_fn ненадёжен (OCR может ошибиться в одной цифре
    # или сгаллюцинировать). photo_ocr всегда идёт в composite-ветку и НЕ пишет
    # kkt_fn (см. INSERT ниже) — OCR-извлечённый номер остаётся только в
    # raw_data.fn для справки. Надёжный kkt_fn (qr_scan / fns / введённый
    # осознанно в manual) дедупится по фискальному номеру — бессрочно, per-org.
    if effective_kkt_fn and source != "photo_ocr":
        existing = await p.fetchrow("SELECT id FROM receipts WHERE kkt_fn=$1 AND org_id=$2",
                                    effective_kkt_fn, user["org_id"])
        if existing:
            raise HTTPException(status_code=409, detail={"error": "duplicate", "existing_id": existing["id"]})

    category = r.category
    if not category or category == "Не указано":
        category = auto_categorize(r.org)

    # Composite-ветка: чеки без надёжного номера — fn-less (manual без номера)
    # ЛИБО photo_ocr (его номер игнорируем). Матчит ВСЕ чеки с теми же
    # composite-ключами за последние 5 минут, независимо от наличия номера
    # (поэтому здесь нет 'AND kkt_fn IS NULL'). Ловит и двойной тап «Использовать»
    # (дубль id 39/41, 0.19s apart, source=photo_ocr), и случай «qr_scan с верным
    # номером → тот же чек через photo_ocr с OCR-ошибкой в номере».
    # IS NOT DISTINCT FROM — чтобы NULL employee/payment/category матчили NULL
    # (у дубля 39/41 employee=NULL, plain '=' его бы не поймал). Сравниваем с
    # *итоговой* category — той, что реально уйдёт в БД.
    if not effective_kkt_fn or source == "photo_ocr":
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

    # Вариант A: photo_ocr НЕ пишет номер ни в fn, ни в kkt_fn (OCR-номер
    # ненадёжен и остаётся только в raw_data.fn). Надёжные источники пишут в обе
    # колонки — fn для backward-compat на переходный период, kkt_fn как основную.
    if source == "photo_ocr":
        fn_to_save = kkt_fn_to_save = None
    else:
        fn_to_save = kkt_fn_to_save = effective_kkt_fn

    # Парсим FNS raw_data в типизированные колонки (только надёжные источники).
    # Сбой парсинга → пустой dict, INSERT всё равно проходит (best-effort).
    # В лог НЕ пишем raw_data — там fn / ИНН поставщика / имя кассира (152-ФЗ).
    parsed: dict = {}
    if source in ("qr_scan", "fns") and r.raw_data:
        try:
            parsed = parse_fns_response(r.raw_data)
        except Exception as e:  # noqa: BLE001 — парсинг не должен блокировать чек
            logger.warning("FNS parse failed: %s", type(e).__name__)
            parsed = {}

    # kkt_fn колонка пишется из dedup-значения (kkt_fn_to_save), НЕ из parsed —
    # чтобы хранимое значение совпадало с тем, по которому шёл дедуп (ЧП C).
    try:
        async with p.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """INSERT INTO receipts (
                        org_id, date, org, category, payment, amount, employee,
                        fn, kkt_fn, raw_data, source, photo_url,
                        datetime, currency, operation_type, org_legal, org_brand,
                        org_inn, payment_form, payment_detail, card_last4,
                        tax_system, address, vat_20, vat_10, vat_0,
                        kkt_serial, kkt_rn, fd_num, fpd, cashier
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
                        $13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,
                        $27,$28,$29,$30,$31
                    ) RETURNING *""",
                    user["org_id"], r.date, r.org, category, r.payment, r.amount, r.employee,
                    fn_to_save, kkt_fn_to_save, r.raw_data, source, r.photo_url,
                    parsed.get("datetime"), parsed.get("currency"), parsed.get("operation_type"),
                    parsed.get("org_legal"), parsed.get("org_brand"), parsed.get("org_inn"),
                    parsed.get("payment_form"), parsed.get("payment_detail"), parsed.get("card_last4"),
                    parsed.get("tax_system"), parsed.get("address"),
                    parsed.get("vat_20"), parsed.get("vat_10"), parsed.get("vat_0"),
                    parsed.get("kkt_serial"), parsed.get("kkt_rn"), parsed.get("fd_num"),
                    parsed.get("fpd"), parsed.get("cashier"),
                )
                # Позиции — best-effort: вложенная транзакция (SAVEPOINT), чтобы
                # сбой вставки/парсинга позиций откатывал ТОЛЬКО их, а чек оставался.
                if source in ("qr_scan", "fns") and r.raw_data:
                    try:
                        async with conn.transaction():
                            for item in parse_fns_items(r.raw_data):
                                await conn.execute(
                                    """INSERT INTO receipt_items
                                       (receipt_id, position, name, quantity, price, sum, vat_rate)
                                       VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                                    row["id"], item["position"], item["name"],
                                    item["quantity"], item["price"], item["sum"], item["vat_rate"],
                                )
                    except Exception as e:  # noqa: BLE001 — позиции не валят чек
                        logger.warning("FNS items insert failed for receipt %s: %s",
                                       row["id"], type(e).__name__)
    except asyncpg.exceptions.UniqueViolationError:
        # Дедуп выше — per-org (WHERE kkt_fn=$1 AND org_id=$2), а partial-unique
        # индекс receipts_kkt_fn_unique — глобальный. Если один и тот же kkt_fn
        # пришёл в РАЗНЫЕ org, SELECT-дедуп промахнётся, а индекс поймает на
        # INSERT — отдаём 409 вместо 500.
        raise HTTPException(status_code=409, detail={
            "error": "duplicate_kkt_fn",
            "message": "Чек с таким фискальным номером уже зарегистрирован",
        })
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
