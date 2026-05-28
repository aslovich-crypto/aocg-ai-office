"""Receipt OCR via Claude Vision (Haiku 4.5).

Single endpoint POST /api/receipts/ocr/ — accepts a photo of a receipt and
returns structured fields (org/inn/date/amount/items/...) plus an
auto-assigned category. Any failure to extract structured data — bad image,
Claude timeout, Claude API error, or non-JSON response — is folded into the
same low-confidence fallback shape so the client always gets a parseable
object and can keep the manual-entry flow alive (same contract as the FNS
proxy in fns.py).
"""

import base64
import json
import logging
import os
from datetime import datetime
from typing import Optional

from anthropic import APIError, APITimeoutError, AsyncAnthropic
from fastapi import APIRouter, File, HTTPException, UploadFile

from app.categorization import auto_categorize_v2, categorize
from app.parsers.fns_parser import validate_inn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/receipts", tags=["receipts", "ocr"])

OCR_MODEL = "claude-haiku-4-5"
OCR_TIMEOUT_SECONDS = 15.0
OCR_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
OCR_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
PDF_MIME = "application/pdf"
PDF_RENDER_DPI = 200  # first-page render resolution; ~1654×2339 for A4
OCR_MAX_TOKENS = 2048

OCR_PROMPT = """Это фотография БУМАЖНОГО кассового чека. Извлеки данные и верни СТРОГО JSON без markdown, без пояснений.

Все суммы — В РУБЛЯХ, как напечатано на чеке (например 6660.00, а НЕ 666000). Это физический чек: числа на нём в рублях, копейки после точки. В отличие от ФНС-API (где суммы в копейках) — здесь НЕ умножай.

Формат ответа:
{
  "org_legal": "полное юридическое название из шапки чека (ООО ..., ИП ...)",
  "org_brand": "торговое название/бренд/вывеска, если отличается от юр.названия",
  "org_inn": "ИНН поставщика — только цифры, без пробелов (10 или 12 цифр)",
  "address": "адрес расчётов одной строкой",
  "datetime": "дата и время чека в ISO: ГГГГ-ММ-ДДTЧЧ:ММ:СС (без таймзоны)",
  "amount": число — итоговая сумма (ИТОГ) в рублях,
  "currency": "RUB",
  "operation_type": "purchase | refund | expense | expense_refund (по тексту ПРИХОД / ВОЗВРАТ ПРИХОДА / РАСХОД / ВОЗВРАТ РАСХОДА)",
  "payment_form": "cash | card | prepaid | credit (по тексту НАЛИЧНЫМИ / БЕЗНАЛИЧНЫМИ / ПРЕДОПЛАТА(АВАНС) / КРЕДИТ)",
  "payment_detail": "название карты или способ оплаты, если указано",
  "card_last4": "последние 4 цифры карты, если видны",
  "tax_system": "СНО поставщика: osno | usn_income | usn_income_minus_expense | psn | eshn | npd (по строке СНО / система налогообложения)",
  "vat_20": число — сумма НДС 20% в рублях, или null,
  "vat_10": число — сумма НДС 10% в рублях, или null,
  "vat_0": число — сумма «без НДС» в рублях, или null,
  "cashier": "имя кассира, если видно",
  "items": [
    {"position": 1, "name": "название", "quantity": число, "price": число (за единицу, руб), "sum": число (итог по позиции, руб), "vat_rate": "20 | 10 | 0 | null"}
  ],
  "confidence": "high | medium | low — оценка качества распознавания"
}

Правила:
- Если поле не видно или не уверен — поставь null. НЕ угадывай и НЕ выдумывай.
- ИНН — только цифры, без пробелов и кавычек.
- Если значение видно частично или нечётко — confidence: medium или low.
- НЕ извлекай фискальные реквизиты (ФН, ЗН ККТ, РН ККТ, ФД, ФПД), даже если они видны на чеке: это длинные числа, и опечатка в одной цифре делает их бесполезными — пропусти их.
Только JSON."""

# Shape returned when Claude can't read the receipt (timeout, API error,
# unparseable response, missing API key, etc.). Mirrors the prompt's field set
# PLUS the backward-compat aliases the current frontend reads (org/date/time/
# payment_type/inn/nds), so the client never needs separate branches.
OCR_FALLBACK: dict = {
    # rich fields (new data standard)
    "org_legal": None, "org_brand": None, "org_inn": None, "address": None,
    "datetime": None, "amount": None, "currency": None, "operation_type": None,
    "payment_form": None, "payment_detail": None, "card_last4": None,
    "tax_system": None, "vat_20": None, "vat_10": None, "vat_0": None,
    "cashier": None, "items": None,
    # backward-compat aliases consumed by the current frontend (handleOcrFile)
    "org": None, "inn": None, "date": None, "time": None,
    "payment_type": None, "nds": None,
    "confidence": "low", "warnings": [],
}

# Rich fields copied verbatim from Claude's JSON before aliasing.
_OCR_RICH_FIELDS = (
    "org_legal", "org_brand", "org_inn", "address", "amount", "currency",
    "operation_type", "payment_form", "payment_detail", "card_last4",
    "tax_system", "vat_20", "vat_10", "vat_0", "cashier",
)

# Module-level singleton — AsyncAnthropic holds an httpx pool, so reusing it
# across requests avoids per-request connection setup. Initialized lazily so
# import-time succeeds even when ANTHROPIC_API_KEY is unset (matches the rest
# of the app — fns.py is lazy too).
_anthropic_client: Optional[AsyncAnthropic] = None


def _get_anthropic_client() -> AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = AsyncAnthropic()
    return _anthropic_client


def _fallback() -> dict:
    """Fresh copy of OCR_FALLBACK with category filled in for an unknown org."""
    return {**OCR_FALLBACK, "category": auto_categorize_v2(""), "warnings": []}


def _normalize_datetime(value) -> Optional[str]:
    """Coerce a date/datetime string from OCR into ISO 'YYYY-MM-DDTHH:MM:SS'.
    Accepts ISO, 'DD.MM.YYYY HH:MM', 'DD.MM.YYYY', 'YYYY-MM-DD', etc. None on failure."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
                "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v).isoformat()
    except ValueError:
        return None


def _finalize_items(items) -> Optional[list]:
    if not isinstance(items, list):
        return None
    out = []
    for pos, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        s = item.get("sum")
        out.append({
            "position": item.get("position", pos),
            "name": item.get("name"),
            "quantity": item.get("quantity"),
            "price": item.get("price"),
            "sum": s,
            "total": s,                 # back-compat alias for the old "total" key
            "vat_rate": item.get("vat_rate"),
        })
    return out


def _finalize(parsed: dict) -> dict:
    """Take Claude's rich JSON and produce the response: rich fields + the
    backward-compat aliases the current frontend reads. Never raises."""
    out = {**OCR_FALLBACK, "warnings": []}
    for k in _OCR_RICH_FIELDS:
        out[k] = parsed.get(k)
    out["confidence"] = parsed.get("confidence") or "low"
    out["currency"] = out["currency"] or "RUB"

    # datetime → ISO; derive back-compat date/time
    iso = _normalize_datetime(parsed.get("datetime"))
    out["datetime"] = iso
    if iso and "T" in iso:
        out["date"], out["time"] = iso.split("T", 1)

    # INN checksum: drop an invalid OCR-read INN and warn (don't store garbage)
    inn = out["org_inn"]
    if inn is not None:
        if validate_inn(inn):
            out["org_inn"] = str(inn).strip()
        else:
            out["org_inn"] = None
            out["warnings"].append("OCR-извлечённый ИНН невалиден, проверьте вручную")

    out["items"] = _finalize_items(parsed.get("items"))

    # Backward-compat aliases for the current frontend (handleOcrFile).
    org = out["org_brand"] or out["org_legal"]
    out["org"] = org
    out["inn"] = out["org_inn"]
    out["payment_type"] = out["payment_form"] if out["payment_form"] in ("cash", "card") else None
    out["category"] = categorize(org or "", out["items"] or [])
    # nds = фактический НДС (20% + 10%); vat_0 — это «без НДС», в сумму не входит.
    vat_parts = [v for v in (out["vat_20"], out["vat_10"]) if isinstance(v, (int, float))]
    out["nds"] = sum(vat_parts) if vat_parts else None
    return out


def _pdf_first_page_to_jpeg(data: bytes) -> bytes:
    """Render page 1 of a PDF to JPEG bytes so the rest of the OCR path can
    treat it like any uploaded image. fitz is imported lazily — a missing
    PyMuPDF only breaks PDF uploads, not app startup (same defensive style as
    the lazy Anthropic client)."""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        if doc.page_count == 0:
            raise ValueError("PDF has no pages")
        pix = doc.load_page(0).get_pixmap(dpi=PDF_RENDER_DPI)
        return pix.tobytes("jpeg")
    finally:
        doc.close()


def _extract_json(text: str) -> Optional[dict]:
    """Pull the JSON object out of Claude's text response.

    The prompt forbids markdown, but defensively strip code fences and slice
    from the first '{' to the last '}' before parsing — cheap insurance
    against the occasional ```json ... ``` wrapper.
    """
    if not text:
        return None
    stripped = text.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    candidate = stripped[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


@router.post("/ocr/")
async def ocr_receipt(file: UploadFile = File(...)):
    """Extract structured receipt data from a photo (or PDF) via Claude Vision."""
    is_pdf = file.content_type == PDF_MIME
    if not file.content_type or (file.content_type not in OCR_ALLOWED_MIME and not is_pdf):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported type {file.content_type!r}; expected JPEG, PNG, WEBP, or PDF",
        )

    # Read one byte past the limit so we can distinguish "exactly at limit"
    # from "too large" without slurping the whole file first.
    data = await file.read(OCR_MAX_FILE_SIZE + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > OCR_MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds {OCR_MAX_FILE_SIZE // (1024 * 1024)} MB limit",
        )

    # Electronic receipts often arrive as PDF (email receipts, "Мой налог",
    # hotel/taxi invoices). Flatten the first page to JPEG and fall through to
    # the same Vision path. media_type then describes the rendered image.
    media_type = file.content_type
    if is_pdf:
        try:
            data = _pdf_first_page_to_jpeg(data)
            media_type = "image/jpeg"
        except Exception as e:  # noqa: BLE001 — bad/empty/encrypted PDF → manual fallback
            logger.warning("OCR PDF conversion failed: %s", type(e).__name__)
            return _fallback()

    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.warning("OCR: ANTHROPIC_API_KEY not set — returning low confidence")
        return _fallback()

    image_b64 = base64.standard_b64encode(data).decode("utf-8")
    client = _get_anthropic_client().with_options(
        timeout=OCR_TIMEOUT_SECONDS,
        max_retries=0,  # 15s is the user-facing budget — no SDK retries inside it.
    )

    try:
        response = await client.messages.create(
            model=OCR_MODEL,
            max_tokens=OCR_MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": OCR_PROMPT},
                    ],
                }
            ],
        )
    except APITimeoutError:
        logger.warning("OCR Claude API timeout")
        return _fallback()
    except APIError as e:
        logger.warning("OCR Claude API error: %s", type(e).__name__)
        return _fallback()
    except Exception as e:  # noqa: BLE001  — last-resort guard; never 500 to the client
        logger.warning("OCR unexpected error: %s", type(e).__name__)
        return _fallback()

    text = next((block.text for block in response.content if block.type == "text"), "")
    parsed = _extract_json(text)
    if parsed is None:
        # 152-ФЗ: НЕ логируем содержимое ответа Claude — там могут быть ИНН/суммы.
        logger.warning("OCR returned non-JSON, length=%d", len(text))
        return _fallback()

    result = _finalize(parsed)
    # Echo the original photo back as base64 so the client can include it in
    # the eventual POST /api/receipts/ body (it lands in raw_data.photo_base64
    # and is served back via GET /api/receipts/{id}/photo). Temporary path
    # before Cloudflare R2 is wired in — once R2 exists this becomes a URL
    # via the receipts.photo_url column.
    result["photo_base64"] = image_b64
    return result
