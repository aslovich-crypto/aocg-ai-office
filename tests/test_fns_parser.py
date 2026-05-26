"""Unit tests for the FNS raw_data parsers (app/parsers/).

Pure functions — no DB, no fixtures. Amounts in the input are kopecks.
"""

from datetime import datetime

from app.parsers.fns_parser import parse_fns_response, validate_inn
from app.parsers.items_parser import parse_fns_items


# A realistic inner FNS json (the object the frontend stores in raw_data).
FULL_RAW = {
    "user": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "АСТЕР"',
    "userInn": "7707083893",                 # valid 10-digit INN (Сбербанк)
    "retailPlace": "Аптека №1",
    "retailPlaceAddress": "Москва, ул. Ленина, д. 1",
    "dateTime": "2026-05-20T13:42:00",
    "operationType": 1,
    "totalSum": 295500,
    "cashTotalSum": 0,
    "ecashTotalSum": 295500,
    "nds20": 49250,
    "nds10": 0,
    "appliedTaxationType": 2,
    "fiscalDriveNumber": "7380440700123456",
    "fiscalDocumentNumber": 1234,
    "fiscalSign": 987654321,
    "kktRegId": "0001234567012345",
    "operator": "Иванова И.И.",
    "items": [
        {"name": "Аспирин", "quantity": 2, "price": 100000, "sum": 200000, "nds": 1},
        {"name": "Бинт", "quantity": 1.5, "price": 63666, "sum": 95500, "nds": 2},
    ],
}


# ─── A. full parse ────────────────────────────────────────────────────
def test_parse_fns_response_full():
    p = parse_fns_response(FULL_RAW)
    assert p["org_legal"].startswith("ОБЩЕСТВО")
    assert p["org_brand"] == "Аптека №1"
    assert p["org_inn"] == "7707083893"
    assert p["address"] == "Москва, ул. Ленина, д. 1"
    assert isinstance(p["datetime"], datetime) and p["datetime"].day == 20
    assert p["currency"] == "RUB"
    assert p["operation_type"] == "purchase"
    assert p["payment_form"] == "card"          # ecashTotalSum > 0
    assert p["tax_system"] == "usn_income"       # appliedTaxationType == 2
    assert p["vat_20"] == 492.50                 # 49250 kopecks
    assert p["vat_10"] == 0.0
    assert p["kkt_fn"] == "7380440700123456"     # fiscalDriveNumber
    assert p["kkt_rn"] == "0001234567012345"     # kktRegId (РН)
    assert p["kkt_serial"] is None               # no kktNumber → ЗН unknown
    assert p["fd_num"] == "1234"
    assert p["fpd"] == "987654321"               # fiscalSign only
    assert p["cashier"] == "Иванова И.И."
    assert p["card_last4"] is None               # FNS never exposes PAN


# ─── B. missing fields → None ─────────────────────────────────────────
def test_parse_fns_response_missing_fields():
    p = parse_fns_response({"user": "ИП Петров", "totalSum": 100})
    assert p["org_legal"] == "ИП Петров"
    assert p["org_inn"] is None
    assert p["address"] is None
    assert p["datetime"] is None
    assert p["vat_20"] is None
    assert p["kkt_fn"] is None
    assert p["cashier"] is None


# ─── C. empty / non-dict → defaults, never raises ─────────────────────
def test_parse_fns_response_empty():
    p = parse_fns_response({})
    assert p["currency"] == "RUB"
    assert p["operation_type"] == "purchase"     # default
    assert p["org_inn"] is None and p["datetime"] is None and p["kkt_fn"] is None
    assert parse_fns_response(None) == {}         # non-dict guarded


# ─── D. valid INN checksums ───────────────────────────────────────────
def test_validate_inn_correct():
    assert validate_inn("7707083893") is True     # 10-digit
    assert validate_inn("500100732259") is True    # 12-digit
    assert validate_inn(7707083893) is True         # int accepted


# ─── E. invalid INN ───────────────────────────────────────────────────
def test_validate_inn_invalid():
    assert validate_inn("7707083894") is False     # bad 10-digit check
    assert validate_inn("500100732250") is False    # bad 12-digit check
    assert validate_inn("123") is False             # wrong length
    assert validate_inn("abcdefghij") is False      # non-digit
    assert validate_inn(None) is False


# ─── F. items parse ───────────────────────────────────────────────────
def test_parse_fns_items_full():
    items = parse_fns_items(FULL_RAW)
    assert len(items) == 2
    assert items[0] == {"position": 1, "name": "Аспирин", "quantity": 2.0,
                        "price": 1000.0, "sum": 2000.0, "vat_rate": "20"}
    assert items[1]["position"] == 2
    assert items[1]["name"] == "Бинт"
    assert items[1]["quantity"] == 1.5
    assert items[1]["sum"] == 955.0
    assert items[1]["vat_rate"] == "10"            # nds code 2 → 10%


# ─── G. no items → [] ─────────────────────────────────────────────────
def test_parse_fns_items_empty():
    assert parse_fns_items({}) == []
    assert parse_fns_items({"items": None}) == []
    assert parse_fns_items(None) == []


# ─── H. operationType variants ────────────────────────────────────────
def test_parse_operation_type():
    op = lambda v: parse_fns_response({"operationType": v})["operation_type"]
    assert op(1) == "purchase"
    assert op(2) == "refund"
    assert op(3) == "expense"
    assert op(4) == "expense_refund"
    assert op("Возврат прихода") == "refund"        # string label accepted
    assert op(99) == "purchase"                      # unknown → default


# ─── I. payment_form by sums ──────────────────────────────────────────
def test_parse_payment_form():
    pf = lambda raw: parse_fns_response(raw)["payment_form"]
    assert pf({"cashTotalSum": 500, "ecashTotalSum": 0}) == "cash"
    assert pf({"cashTotalSum": 0, "ecashTotalSum": 500}) == "card"
    assert pf({"prepaidSum": 500}) == "prepaid"
    assert pf({"creditSum": 500}) == "credit"
    assert pf({"cashTotalSum": 0, "ecashTotalSum": 0}) is None
