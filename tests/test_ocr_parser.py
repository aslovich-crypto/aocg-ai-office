"""Unit tests for the photo-OCR raw_data parser (app/parsers/ocr_parser.py +
parse_ocr_items). Pure functions — no DB, no fixtures.

Unlike FNS, OCR amounts are already in RUBLES (never divided), vat_rate arrives
as a string, and kkt_fn is always None (Вариант A). FULL_OCR mirrors the real
production payload of receipt id=3 (a finalized Claude Vision result), plus NDS
values so the rubles-not-kopecks behaviour is exercised.
"""

from datetime import datetime

from app.parsers.fns_parser import parse_fns_response
from app.parsers.items_parser import parse_ocr_items
from app.parsers.ocr_parser import parse_ocr_response


# Real prod shape (id=3) — the dict ocr.py's _finalize produces, incl. aliases.
FULL_OCR = {
    "org_legal": 'ООО "МЕРЕ"',
    "org_brand": 'Ресторан "Мере"',
    "org_inn": "7813679582",                 # already validated in _finalize
    "address": "197110, Санкт-Петербург, ул. Ломейновольская, д. 7",
    "datetime": "2026-05-26T12:41:00",        # ISO string from _finalize
    "amount": 1010.0,
    "currency": "RUB",
    "operation_type": "purchase",             # already a string, not an int code
    "payment_form": "card",
    "payment_detail": None,
    "card_last4": None,
    "tax_system": "osno",
    "vat_20": 1110.00,                        # RUBLES — must NOT become 11.10
    "vat_10": 222.00,
    "vat_0": None,
    "cashier": "Ботина Анастасия",
    "items": [
        {"position": 1, "name": "Эспрессо 40мл", "quantity": 1, "price": 250, "sum": 250, "vat_rate": "20"},
        {"position": 2, "name": "Зеленая греча", "quantity": 1, "price": 760, "sum": 760, "vat_rate": "10"},
    ],
    "confidence": "high",
    # backward-compat aliases _finalize also adds — parser must ignore them cleanly
    "org": 'Ресторан "Мере"', "inn": "7813679582", "date": "2026-05-26",
    "time": "12:41:00", "payment_type": "card", "nds": None,
}


# ─── A. full parse ────────────────────────────────────────────────────
def test_parse_ocr_response_full():
    p = parse_ocr_response(FULL_OCR)
    assert p["org_legal"] == 'ООО "МЕРЕ"'
    assert p["org_brand"] == 'Ресторан "Мере"'
    assert p["org_inn"] == "7813679582"
    assert p["address"].startswith("197110")
    assert p["currency"] == "RUB"
    assert p["operation_type"] == "purchase"
    assert p["payment_form"] == "card"
    assert p["tax_system"] == "osno"
    assert p["cashier"] == "Ботина Анастасия"
    assert p["vat_20"] == 1110.00              # rubles preserved
    assert p["vat_10"] == 222.00
    # OCR never trusts fiscal data — all None (Вариант A)
    assert p["kkt_fn"] is None
    assert p["kkt_serial"] is None and p["kkt_rn"] is None
    assert p["fd_num"] is None and p["fpd"] is None


# ─── J. datetime ISO string → datetime object (fits TIMESTAMP column) ──
def test_parse_ocr_response_datetime_to_object():
    p = parse_ocr_response(FULL_OCR)
    assert isinstance(p["datetime"], datetime)   # NOT a str — asyncpg needs an object
    assert p["datetime"].year == 2026 and p["datetime"].month == 5 and p["datetime"].day == 26
    assert p["datetime"].hour == 12 and p["datetime"].minute == 41


# ─── B. missing optional fields → None, never raises ──────────────────
def test_parse_ocr_response_missing_optional():
    p = parse_ocr_response({"org_legal": "ИП Тест", "amount": 100})
    assert p["org_legal"] == "ИП Тест"
    assert p["org_inn"] is None
    assert p["address"] is None
    assert p["datetime"] is None
    assert p["vat_20"] is None and p["vat_10"] is None and p["vat_0"] is None
    assert p["tax_system"] is None
    assert p["cashier"] is None


# ─── C. empty / non-dict → defaults, never raises ─────────────────────
def test_parse_ocr_response_empty():
    p = parse_ocr_response({})
    assert p["currency"] == "RUB"
    assert p["operation_type"] == "purchase"     # default
    assert p["org_inn"] is None and p["datetime"] is None and p["kkt_fn"] is None
    assert parse_ocr_response(None) == {}         # non-dict guarded
    assert parse_ocr_response("not a dict") == {}


# ─── D. kkt_fn ALWAYS None — even when OCR hallucinated one ───────────
def test_parse_ocr_response_kkt_fn_always_none():
    p = parse_ocr_response({**FULL_OCR, "kkt_fn": "OCR_HALLUCINATED_FN",
                            "fiscalDriveNumber": "1234567890123456"})
    assert p["kkt_fn"] is None                    # Вариант A — never written from OCR


# ─── E. header amounts stay in RUBLES (not divided by 100) ────────────
def test_parse_ocr_response_amounts_in_rubles():
    p = parse_ocr_response({"vat_20": 1110.00, "vat_10": 55.50})
    assert p["vat_20"] == 1110.00                 # NOT 11.10
    assert p["vat_10"] == 55.50                    # NOT 0.555


# ─── K. key set matches parse_fns_response → shared INSERT is safe ────
def test_parse_ocr_response_keys_match_fns():
    ocr_keys = set(parse_ocr_response(FULL_OCR).keys())
    fns_keys = set(parse_fns_response({"user": "X", "totalSum": 1}).keys())
    assert ocr_keys == fns_keys                   # one INSERT serves both sources


# ─── F. items parse — rubles + vat_rate string ───────────────────────
def test_parse_ocr_items_full():
    items = parse_ocr_items(FULL_OCR)
    assert len(items) == 2
    assert items[0] == {"position": 1, "name": "Эспрессо 40мл", "quantity": 1.0,
                        "price": 250.0, "sum": 250.0, "vat_rate": "20"}
    assert items[1]["position"] == 2
    assert items[1]["name"] == "Зеленая греча"
    assert items[1]["sum"] == 760.0
    assert items[1]["vat_rate"] == "10"


# ─── H. item amounts stay in RUBLES (not divided by 100) ──────────────
def test_parse_ocr_items_amounts_in_rubles():
    items = parse_ocr_items({"items": [
        {"name": "Кофе", "quantity": 2, "price": 250, "sum": 500, "vat_rate": "20"}]})
    assert items[0]["price"] == 250.0             # NOT 2.50
    assert items[0]["sum"] == 500.0                # NOT 5.00
    assert items[0]["quantity"] == 2.0


# ─── I. vat_rate taken as a string, not decoded via _VAT_RATE_BY_CODE ─
def test_parse_ocr_items_vat_rate_string():
    items = parse_ocr_items({"items": [
        {"name": "X", "vat_rate": "20"}, {"name": "Y", "vat_rate": None}]})
    assert items[0]["vat_rate"] == "20"            # kept as-is (FNS code path would give None)
    assert items[1]["vat_rate"] is None


# ─── G. no items → [] ─────────────────────────────────────────────────
def test_parse_ocr_items_empty():
    assert parse_ocr_items({}) == []
    assert parse_ocr_items({"items": None}) == []
    assert parse_ocr_items(None) == []
