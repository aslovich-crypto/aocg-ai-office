"""Unit tests for the FNS raw_data parsers (app/parsers/).

Pure functions — no DB, no fixtures. Amounts in the input are kopecks.
"""

from datetime import datetime

from app.parsers.fns_parser import parse_fns_response, validate_inn
from app.parsers.items_parser import parse_fns_items


# A realistic inner FNS json (the object the frontend stores in raw_data).
FULL_RAW = {
    "user": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "АСТЕР"',
    "userInn": "7707083893",  # valid 10-digit INN (Сбербанк)
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
    assert p["payment_form"] == "card"  # ecashTotalSum > 0
    assert p["tax_system"] == "usn_income"  # appliedTaxationType == 2
    # NDS-CLEANUP ②: колонок ставок больше нет — НДС в разбивке по тегу 1199
    assert p["vat_breakdown"] == {"20": 492.50}
    assert "vat_20" not in p and "vat_10" not in p
    assert p["vat_total"] is None  # у ФНС общая сумма не нужна: есть разбивка
    assert p["kkt_fn"] == "7380440700123456"  # fiscalDriveNumber
    assert p["kkt_rn"] == "0001234567012345"  # kktRegId (РН)
    assert p["kkt_serial"] is None  # no kktNumber → ЗН unknown
    assert p["fd_num"] == "1234"
    assert p["fpd"] == "987654321"  # fiscalSign only
    assert p["cashier"] == "Иванова И.И."
    assert p["card_last4"] is None  # FNS never exposes PAN


# ─── B. missing fields → None ─────────────────────────────────────────
def test_parse_fns_response_missing_fields():
    p = parse_fns_response({"user": "ИП Петров", "totalSum": 100})
    assert p["org_legal"] == "ИП Петров"
    assert p["org_inn"] is None
    assert p["address"] is None
    assert p["datetime"] is None
    assert p["vat_breakdown"] is None and p["vat_total"] is None
    assert p["kkt_fn"] is None
    assert p["cashier"] is None


# ─── C. empty / non-dict → defaults, never raises ─────────────────────
def test_parse_fns_response_empty():
    p = parse_fns_response({})
    assert p["currency"] == "RUB"
    assert p["operation_type"] == "purchase"  # default
    assert p["org_inn"] is None and p["datetime"] is None and p["kkt_fn"] is None
    assert parse_fns_response(None) == {}  # non-dict guarded


# ─── D. valid INN checksums ───────────────────────────────────────────
def test_validate_inn_correct():
    assert validate_inn("7707083893") is True  # 10-digit
    assert validate_inn("500100732259") is True  # 12-digit
    assert validate_inn(7707083893) is True  # int accepted


# ─── E. invalid INN ───────────────────────────────────────────────────
def test_validate_inn_invalid():
    assert validate_inn("7707083894") is False  # bad 10-digit check
    assert validate_inn("500100732250") is False  # bad 12-digit check
    assert validate_inn("123") is False  # wrong length
    assert validate_inn("abcdefghij") is False  # non-digit
    assert validate_inn(None) is False


# ─── F. items parse ───────────────────────────────────────────────────
def test_parse_fns_items_full():
    items = parse_fns_items(FULL_RAW)
    assert len(items) == 2
    assert items[0] == {
        "position": 1,
        "name": "Аспирин",
        "quantity": 2.0,
        "price": 1000.0,
        "sum": 2000.0,
        "vat_rate": "20",
    }
    assert items[1]["position"] == 2
    assert items[1]["name"] == "Бинт"
    assert items[1]["quantity"] == 1.5
    assert items[1]["sum"] == 955.0
    assert items[1]["vat_rate"] == "10"  # nds code 2 → 10%


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
    assert op("Возврат прихода") == "refund"  # string label accepted
    assert op(99) == "purchase"  # unknown → default


# ─── I. payment_form by sums ──────────────────────────────────────────
def test_parse_payment_form():
    pf = lambda raw: parse_fns_response(raw)["payment_form"]
    assert pf({"cashTotalSum": 500, "ecashTotalSum": 0}) == "cash"
    assert pf({"cashTotalSum": 0, "ecashTotalSum": 500}) == "card"
    assert pf({"prepaidSum": 500}) == "prepaid"
    assert pf({"creditSum": 500}) == "credit"
    assert pf({"cashTotalSum": 0, "ecashTotalSum": 0}) is None


# ─── J. НДС НЕ ИСЧЕЗАЕТ (NDS-CLEANUP ②) ───────────────────────────────
# Ловушка названа владельцем ДО реализации и оказалась не гипотетической:
# ровно такой ответ (суммы наверху, позиции без кодов ставок) лежал в фикстуре
# этого файла. После того как колонки vat_20/vat_10 убраны, такой чек остался
# бы без НДС вовсе — молча, потому что «пустая разбивка» и «НДС нет» выглядят
# одинаково.
def test_ндс_из_верхних_полей_когда_позиции_без_кодов():
    p = parse_fns_response(
        {
            "user": "ООО Тест",
            "totalSum": 100000,
            "nds20": 49250,  # копейки
            "nds10": 1000,
            "items": [{"name": "товар", "sum": 100000}],  # кодов ставок НЕТ
        }
    )
    assert p["vat_breakdown"] == {"20": 492.50, "10": 10.0}


def test_ндс_из_позиций_имеет_приоритет_над_верхними():
    """Позиции полнее: там ВСЕ ставки, включая 22%, которой наверху нет."""
    p = parse_fns_response(
        {
            "user": "ООО Тест",
            "nds20": 49250,
            "items": [
                {"name": "а", "nds": 11, "ndsSum": 22000},  # код 11 = 22%
                {"name": "б", "nds": 2, "ndsSum": 1000},  # код 2 = 10%
            ],
        }
    )
    assert p["vat_breakdown"] == {"22": 220.0, "10": 10.0}


def test_ндс_не_исчезает_ни_при_каком_виде_ответа():
    """ИНВАРИАНТ: если в ответе есть НДС, он обязан оказаться в разбивке.

    Проверяется не одна форма, а НАБОР форм — именно потому, что дефект
    возникает на форме, которую не предусмотрели.
    """
    формы = [
        {"nds20": 12000},
        {"nds18": 12000},  # legacy 18% → кладём как 20
        {"nds10": 5000},
        {"items": [{"nds": 1, "ndsSum": 12000}]},
        {"items": [{"nds": 11, "ndsSum": 12000}]},  # 22%
        {"nds20": 12000, "items": [{"name": "без кода", "sum": 1}]},
    ]
    for форма in формы:
        p = parse_fns_response({"user": "X", "totalSum": 1, **форма})
        сумма = sum((p["vat_breakdown"] or {}).values()) + (p["vat_total"] or 0)
        assert сумма > 0, f"НДС потерялся на форме {форма}"


def test_без_ндс_остаётся_без_ндс():
    """Обратная половина: там, где НДС нет, он не должен появляться."""
    p = parse_fns_response({"user": "X", "totalSum": 1, "ndsNo": 10000})
    assert p["vat_breakdown"] is None
    assert p["vat_total"] is None
    assert p["vat_0"] == 100.0  # «без НДС» — отдельное поле, в сумму не входит
