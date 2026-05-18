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
import os
from typing import Optional

from anthropic import APIError, APITimeoutError, AsyncAnthropic
from fastapi import APIRouter, File, HTTPException, UploadFile

from app.categorization import auto_categorize

router = APIRouter(prefix="/api/receipts", tags=["receipts", "ocr"])

OCR_MODEL = "claude-haiku-4-5"
OCR_TIMEOUT_SECONDS = 15.0
OCR_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
OCR_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
OCR_MAX_TOKENS = 2048

OCR_PROMPT = """Это фото кассового чека. Извлеки данные и верни строго в JSON без markdown:
{
  "org": "название организации",
  "inn": "ИНН если есть",
  "address": "адрес если есть",
  "date": "дата в формате YYYY-MM-DD",
  "time": "время HH:MM если есть",
  "amount": число (итоговая сумма),
  "items": [{"name": "название", "qty": число, "price": число, "total": число}],
  "nds": число или null,
  "payment_type": "card" или "cash",
  "fn": "фискальный номер если есть",
  "confidence": "high/medium/low"
}
Если поле не найдено — null. Только JSON, без пояснений."""

# Shape returned when Claude can't read the receipt (timeout, API error,
# unparseable response, missing API key, etc.). Mirrors the field set in the
# prompt so the client doesn't need separate branches for "ok with nulls" vs
# "everything failed".
OCR_FALLBACK: dict = {
    "org": None,
    "inn": None,
    "address": None,
    "date": None,
    "time": None,
    "amount": None,
    "items": None,
    "nds": None,
    "payment_type": None,
    "fn": None,
    "confidence": "low",
}

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
    return {**OCR_FALLBACK, "category": auto_categorize("")}


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
    """Extract structured receipt data from a photo via Claude Vision."""
    if not file.content_type or file.content_type not in OCR_ALLOWED_MIME:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported image type {file.content_type!r}; expected JPEG, PNG, or WEBP",
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

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("[OCR] ANTHROPIC_API_KEY not set — returning low confidence", flush=True)
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
                                "media_type": file.content_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": OCR_PROMPT},
                    ],
                }
            ],
        )
    except APITimeoutError:
        print("[OCR] Claude API timeout", flush=True)
        return _fallback()
    except APIError as e:
        print(f"[OCR] Claude API error: {type(e).__name__}: {e}", flush=True)
        return _fallback()
    except Exception as e:  # noqa: BLE001  — last-resort guard; never 500 to the client
        print(f"[OCR] unexpected error: {type(e).__name__}: {e}", flush=True)
        return _fallback()

    text = next((block.text for block in response.content if block.type == "text"), "")
    parsed = _extract_json(text)
    if parsed is None:
        print(f"[OCR] non-JSON response from Claude: {text[:200]!r}", flush=True)
        return _fallback()

    org = parsed.get("org") or ""
    parsed["category"] = auto_categorize(org) if isinstance(org, str) else "Не указано"
    # Echo the original photo back as base64 so the client can include it in
    # the eventual POST /api/receipts/ body (it lands in raw_data.photo_base64
    # and is served back via GET /api/receipts/{id}/photo). Temporary path
    # before Cloudflare R2 is wired in — once R2 exists this becomes a URL
    # via the receipts.photo_url column.
    parsed["photo_base64"] = image_b64
    return parsed
