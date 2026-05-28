import base64
import binascii
import logging
from datetime import date
from typing import List, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from app.auth import get_current_user
from app.categorization import DEFAULT_FALLBACK, categorize
from app.database import get_pool
from app.parsers.fns_parser import parse_fns_response
from app.parsers.items_parser import parse_fns_items, parse_ocr_items
from app.parsers.ocr_parser import parse_ocr_response

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

def _similar_receipt_brief(row) -> dict:
    """Краткая карточка похожего чека для body.warning (задача №9 фаза A): фронт
    рисует inline-баннер без второго GET. amount: Decimal → float (иначе JSON
    падает); date: datetime.date → ISO-строка. org может быть legacy-строкой."""
    return {
        "id": row["id"],
        "org": row["org"],
        "amount": float(row["amount"]) if row["amount"] is not None else None,
        "date": row["date"].isoformat() if row["date"] else None,
    }


async def resolve_category_id(db, org_id: int, category_name: str):
    """Имя статьи → category_id для конкретной орг (справочник per-org, Фикс №1).
    Если имени нет — фолбэк на «Прочие хозрасходы» той же орг. None, если и фолбэка
    нет (орг ещё не засеяна — напр. legacy/тестовые данные). db = pool или conn."""
    row = await db.fetchrow(
        "SELECT id FROM categories WHERE org_id=$1 AND name=$2 LIMIT 1", org_id, category_name)
    if row:
        return row["id"]
    row = await db.fetchrow(
        "SELECT id FROM categories WHERE org_id=$1 AND name=$2 LIMIT 1", org_id, DEFAULT_FALLBACK)
    return row["id"] if row else None


def _dup_item(row, *, is_new, in_report) -> dict:
    """Элемент warning.duplicates (задача №9 фаза C). deletable = нет надёжного
    фискального номера (kkt_fn IS NULL → photo_ocr/manual): фронт по нему ставит
    галочку «удалить» по умолчанию, у ФНС/qr_scan — снимает (юр. сила). amount:
    Decimal→float, date: date→ISO. in_report/is_new передаёт вызывающий."""
    return {
        "id": row["id"],
        "org": row["org"],
        "amount": float(row["amount"]) if row["amount"] is not None else None,
        "date": row["date"].isoformat() if row["date"] else None,
        "source": row["source"],
        "deletable": row["kkt_fn"] is None,
        "in_report": in_report,
        "is_new": is_new,
    }


@router.post("/")
async def create_receipt(r: ReceiptIn, user: dict = Depends(get_current_user)):
    p = await get_pool()
    source = r.source or DEFAULT_SOURCE
    effective_kkt_fn = r.kkt_fn or r.fn   # переходный fallback: фронт пока шлёт fn
    org_id = user["org_id"]

    # ── Парсинг raw_data ДО категоризации и дедупа: даёт org_brand (приоритет бренда,
    # Фикс A2) и effective_org_inn (composite-ветки). ФНС-формат для qr_scan/fns,
    # OCR-формат для photo_ocr; оба парсера возвращают ОДИН набор ключей (см. INSERT).
    # Сбой → {}, чек всё равно создастся. В лог НЕ пишем raw_data (ИНН/кассир, 152-ФЗ).
    parsed: dict = {}
    if r.raw_data:
        try:
            if source in ("qr_scan", "fns"):
                parsed = parse_fns_response(r.raw_data)
            elif source == "photo_ocr":
                parsed = parse_ocr_response(r.raw_data)
        except Exception as e:  # noqa: BLE001 — парсинг не должен блокировать чек
            logger.warning("raw_data parse failed (source=%s): %s", source, type(e).__name__)
            parsed = {}
    effective_org_inn = parsed.get("org_inn")   # уже провалидирован в parse_*_response

    # Категория: если пользователь не задал (или «Не указано») — авто. Приоритет
    # (Фикс №4 + A2): позиции → бренд (org_brand) → юрлицо (org) → «Прочие хозрасходы».
    # Строку category пишем как раньше (backward compat) + резолвим в category_id per-org.
    category = r.category
    if not category or category == "Не указано":
        items = []
        if r.raw_data:
            if source in ("qr_scan", "fns"):
                items = parse_fns_items(r.raw_data)
            elif source == "photo_ocr":
                items = parse_ocr_items(r.raw_data)
        category = categorize(r.org or "", items, brand=parsed.get("org_brand"))
    category_id = await resolve_category_id(p, org_id, category)

    # Надёжный фискальный номер есть только у не-photo_ocr источников с номером.
    # photo_ocr пишет kkt_fn=NULL (Вариант A) — его OCR-номер не считается надёжным.
    has_reliable_fn = bool(effective_kkt_fn) and source != "photo_ocr"

    # Дедуп — 4 ветки в строгом порядке. Жёсткий 409 только в ветках 0 и 1
    # (точные совпадения); ветки 2/3 — мягкое предупреждение (чек создаётся),
    # «лучше лишний чек, чем потерянный» (диагностика 26.05, фиксы C1/C2/C3).

    # ── Ветка 0 — двойной тап (90 сек). Только для fn-less чеков: у источников
    # с надёжным fn повторный тап = тот же fn и ловится веткой 1. Защита от дублей,
    # пока фронт не показывает warning (задача №9).
    if not has_reliable_fn:
        dbl = await p.fetchrow(
            """SELECT id FROM receipts
               WHERE date = $1 AND amount = $2 AND org_id = $3 AND source = $4
                 AND kkt_fn IS NULL
                 AND created_at > NOW() - INTERVAL '90 seconds'
               LIMIT 1""",
            r.date, r.amount, org_id, source,
        )
        if dbl:
            raise HTTPException(status_code=409, detail={
                "error": "double_tap_detected",
                "message": "Похожий чек только что добавлен. Подождите 90 секунд или измените данные.",
                "existing_id": dbl["id"],
            })

    # ── Ветка 1 — точный дубль по kkt_fn (per-org, бессрочно). ФНС-номер уникален.
    if has_reliable_fn:
        existing = await p.fetchrow(
            "SELECT id FROM receipts WHERE kkt_fn=$1 AND org_id=$2",
            effective_kkt_fn, org_id,
        )
        if existing:
            raise HTTPException(status_code=409, detail={
                "error": "duplicate_kkt_fn",
                "message": "Чек с таким фискальным номером уже зарегистрирован",
                "existing_id": existing["id"],
            })

    # ── Ветки 2/3 — мягкое предупреждение по composite, окно 7 дней. Срабатывает
    # ровно одна (взаимоисключающие): сильная (есть ИНН) ИЛИ слабая (нет ИНН).
    # fn-фильтр динамический ($N = has_reliable_fn): чек с надёжным fn ищет дубль
    # ТОЛЬКО среди fn-less чеков (иначе два РАЗНЫХ qr с разными fn ложно совпали бы
    # по дате+сумме+ИНН); fn-less чек ищет среди всех — так ловятся ОБА направления
    # реального бага id3↔id4 (photo_ocr↔qr_scan). category/payment в ключ НЕ входят
    # (C3): пользователь меняет их после создания, ключ бы рассинхронизировался.
    # Собираем ВСЕ совпадения в окне 7 дней (fetch, не fetchrow): фронт покажет
    # их группой с чекбоксами для удаления лишних (задача №9 фаза C). Запрос ДО
    # INSERT — только что созданный чек добавим в массив явно после вставки (под
    # динамическим fn-фильтром он бы не попал). SELECT тянет source/kkt_fn (для
    # deletable) + EXISTS(report_items) AS in_report. created_at ASC — хронология.
    # Срабатывает ровно одна ветка: сильная (есть ИНН) ИЛИ слабая (нет ИНН).
    dup_confidence = dup_message = None
    similar_rows = []
    if effective_org_inn:
        similar_rows = await p.fetch(
            """SELECT id, org, amount, date, source, kkt_fn,
                      EXISTS(SELECT 1 FROM report_items ri WHERE ri.receipt_id = receipts.id) AS in_report
               FROM receipts
               WHERE date = $1 AND amount = $2 AND org_inn = $3 AND org_id = $4
                 AND (NOT $5::boolean OR kkt_fn IS NULL)
                 AND created_at > NOW() - INTERVAL '7 days'
               ORDER BY created_at ASC""",
            r.date, r.amount, effective_org_inn, org_id, has_reliable_fn,
        )
        if similar_rows:
            dup_confidence = "high"
            dup_message = "Возможный дубль: дата, сумма и ИНН поставщика совпадают с чеком за последние 7 дней"
    else:
        similar_rows = await p.fetch(
            """SELECT id, org, amount, date, source, kkt_fn,
                      EXISTS(SELECT 1 FROM report_items ri WHERE ri.receipt_id = receipts.id) AS in_report
               FROM receipts
               WHERE date = $1 AND amount = $2 AND org_id = $3
                 AND (NOT $4::boolean OR kkt_fn IS NULL)
                 AND created_at > NOW() - INTERVAL '7 days'
               ORDER BY created_at ASC""",
            r.date, r.amount, org_id, has_reliable_fn,
        )
        if similar_rows:
            dup_confidence = "low"
            dup_message = "Возможный дубль: дата и сумма совпадают с чеком за последние 7 дней"

    # Вариант A: photo_ocr НЕ пишет номер ни в fn, ни в kkt_fn (OCR-номер
    # ненадёжен и остаётся только в raw_data.fn). Надёжные источники пишут в обе
    # колонки — fn для backward-compat на переходный период, kkt_fn как основную.
    if source == "photo_ocr":
        fn_to_save = kkt_fn_to_save = None
    else:
        fn_to_save = kkt_fn_to_save = effective_kkt_fn

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
                        kkt_serial, kkt_rn, fd_num, fpd, cashier, category_id
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
                        $13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,
                        $27,$28,$29,$30,$31,$32
                    ) RETURNING *""",
                    user["org_id"], r.date, r.org, category, r.payment, r.amount, r.employee,
                    fn_to_save, kkt_fn_to_save, r.raw_data, source, r.photo_url,
                    parsed.get("datetime"), parsed.get("currency"), parsed.get("operation_type"),
                    parsed.get("org_legal"), parsed.get("org_brand"), parsed.get("org_inn"),
                    parsed.get("payment_form"), parsed.get("payment_detail"), parsed.get("card_last4"),
                    parsed.get("tax_system"), parsed.get("address"),
                    parsed.get("vat_20"), parsed.get("vat_10"), parsed.get("vat_0"),
                    parsed.get("kkt_serial"), parsed.get("kkt_rn"), parsed.get("fd_num"),
                    parsed.get("fpd"), parsed.get("cashier"), category_id,
                )
                # Позиции — best-effort: вложенная транзакция (SAVEPOINT), чтобы
                # сбой вставки/парсинга позиций откатывал ТОЛЬКО их, а чек оставался.
                # Выбор парсера по источнику (ФНС-коды vs OCR-строки, копейки vs рубли).
                if r.raw_data and source in ("qr_scan", "fns", "photo_ocr"):
                    parse_items = (parse_fns_items if source in ("qr_scan", "fns")
                                   else parse_ocr_items)
                    try:
                        async with conn.transaction():
                            for item in parse_items(r.raw_data):
                                await conn.execute(
                                    """INSERT INTO receipt_items
                                       (receipt_id, position, name, quantity, price, sum, vat_rate)
                                       VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                                    row["id"], item["position"], item["name"],
                                    item["quantity"], item["price"], item["sum"], item["vat_rate"],
                                )
                    except Exception as e:  # noqa: BLE001 — позиции не валят чек
                        logger.warning("items insert failed for receipt %s (source=%s): %s",
                                       row["id"], source, type(e).__name__)
    except asyncpg.exceptions.UniqueViolationError:
        # Дедуп выше — per-org (WHERE kkt_fn=$1 AND org_id=$2), а partial-unique
        # индекс receipts_kkt_fn_unique — глобальный. Если один и тот же kkt_fn
        # пришёл в РАЗНЫЕ org, SELECT-дедуп промахнётся, а индекс поймает на
        # INSERT — отдаём 409 вместо 500.
        raise HTTPException(status_code=409, detail={
            "error": "duplicate_kkt_fn_cross_org",
            "message": "Чек с таким фискальным номером уже зарегистрирован в другой организации",
        })
    result = dict(row)
    if dup_confidence:
        # duplicates = существующие совпадения (created_at ASC) + только что
        # созданный чек (is_new=true, in_report=false: он ещё ни в одном отчёте).
        # similar_receipt[_id] — deprecated-поля для старого фронта (фаза A),
        # указывают на первый существующий дубль; фронт переходит на duplicates.
        duplicates = [_dup_item(s, is_new=False, in_report=bool(s["in_report"])) for s in similar_rows]
        duplicates.append(_dup_item(row, is_new=True, in_report=False))
        result["warning"] = {
            "type": "possible_duplicate",
            "confidence": dup_confidence,
            "message": dup_message,
            "similar_receipt_id": similar_rows[0]["id"],                 # deprecated
            "similar_receipt": _similar_receipt_brief(similar_rows[0]),  # deprecated
            "duplicates": duplicates,
        }
    return result

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


class BulkDeleteIn(BaseModel):
    ids: List[int]
    force: bool = False        # пробивает ТОЛЬКО ФНС-защиту, НЕ in_report (Q2)


@router.post("/bulk-delete")
async def bulk_delete_receipts(body: BulkDeleteIn, user: dict = Depends(get_current_user)):
    """Массовое удаление дублей из баннера (задача №9 фаза C). Защиты:
    • in_report — блок ВСЕГДА (нельзя молча выкинуть чек из отчёта), force не пробивает;
    • надёжный фискальный номер (kkt_fn) — блок без force (у ФНС/qr_scan юр. сила);
    • чужие id молча отсеиваются (изоляция по org_id).
    Ответ всегда 200 с детализацией {deleted, blocked_fns, blocked_in_report}."""
    org_id = user["org_id"]
    deleted, blocked_fns, blocked_in_report = [], [], []
    if not body.ids:
        return {"deleted": deleted, "blocked_fns": blocked_fns, "blocked_in_report": blocked_in_report}
    p = await get_pool()
    # Кандидаты — только чеки текущей орг; чужие id в выборку не попадают (изоляция).
    rows = await p.fetch(
        """SELECT id, kkt_fn,
                  EXISTS(SELECT 1 FROM report_items ri WHERE ri.receipt_id = receipts.id) AS in_report
           FROM receipts WHERE id = ANY($1::int[]) AND org_id = $2""",
        body.ids, org_id,
    )
    for row in rows:
        if row["in_report"]:
            blocked_in_report.append(row["id"])              # блок ВСЕГДА
        elif row["kkt_fn"] is not None and not body.force:
            blocked_fns.append(row["id"])                    # ФНС-защита, пробивается force
        else:
            deleted.append(row["id"])
    if deleted:
        async with p.acquire() as conn:
            async with conn.transaction():
                # org-безопасная чистка связей (закрывает дыру изоляции 1.2):
                # report_items трогаем ТОЛЬКО для чеков своей орг.
                await conn.execute(
                    """DELETE FROM report_items WHERE receipt_id = ANY($1::int[])
                       AND receipt_id IN (SELECT id FROM receipts WHERE org_id = $2)""",
                    deleted, org_id,
                )
                await conn.execute(
                    "DELETE FROM receipts WHERE id = ANY($1::int[]) AND org_id = $2",
                    deleted, org_id,
                )
    return {"deleted": deleted, "blocked_fns": blocked_fns, "blocked_in_report": blocked_in_report}


class ReceiptPatch(BaseModel):
    category: Optional[str] = None
    payment: Optional[str] = None
    org: Optional[str] = None

@router.patch("/{id}")
async def patch_receipt(id: int, r: ReceiptPatch, user: dict = Depends(get_current_user)):
    p = await get_pool()
    org_id = user["org_id"]
    fields, values = [], []
    for k, v in [("category", r.category), ("payment", r.payment), ("org", r.org)]:
        if v is not None:
            values.append(v)
            fields.append(f"{k}=${len(values)}")
    # Ручная смена категории: category_id резолвим server-side (per-org, НЕ из тела —
    # IDOR-защита) и ставим category_manual=TRUE — будущий батч-пересчёт (Фикс №4)
    # такой чек не тронет. Триггер — любой непустой category в патче (явный выбор).
    if r.category:
        values.append(await resolve_category_id(p, org_id, r.category))
        fields.append(f"category_id=${len(values)}")
        values.append(True)
        fields.append(f"category_manual=${len(values)}")
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
