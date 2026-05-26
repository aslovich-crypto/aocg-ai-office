"""Parse a proverkacheka/FNS receipt json (the inner `data.json` object that the
frontend stores in raw_data) into typed columns for the receipts table.

Defensive by design: every field goes through `.get()` and a tolerant coercion,
so a missing or malformed value yields ``None`` rather than raising — a broken
FNS payload must never block receipt creation. Monetary amounts arrive from the
FNS in kopecks and are divided by 100.
"""

from datetime import datetime, timezone
from typing import Optional

# operationType: FNS tag 1054. Comes as an int (1..4) but some providers echo
# the Russian label — accept both.
_OPERATION_TYPES = {
    1: "purchase", 2: "refund", 3: "expense", 4: "expense_refund",
    "Приход": "purchase", "Возврат прихода": "refund",
    "Расход": "expense", "Возврат расхода": "expense_refund",
}

# taxationType / appliedTaxationType: FNS tag 1055, a bitmask. Lowest set bit wins.
_TAXATION_TYPES = {
    1: "osno", 2: "usn_income", 4: "usn_income_minus_expense",
    8: "envd", 16: "eshn", 32: "psn", 64: "npd",
}

# Per-item nds code (FNS tag 1199): 1=20%, 2=10%, 3=20/120, 4=10/110, 5=0%, 6=без НДС.
_VAT_RATE_BY_CODE = {1: "20", 2: "10", 3: "20", 4: "10", 5: "0", 6: "0"}


def _str_or_none(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _kopecks(value) -> Optional[float]:
    """kopecks (int/str/float) → rubles, or None."""
    if value is None:
        return None
    try:
        return round(float(value) / 100, 2)
    except (TypeError, ValueError):
        return None


def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_inn(inn) -> bool:
    """Russian INN checksum validation (10- or 12-digit). False for anything
    that isn't structurally a valid INN."""
    if inn is None:
        return False
    s = str(inn).strip()
    if not s.isdigit():
        return False
    if len(s) == 10:
        coef = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        check = sum(c * int(d) for c, d in zip(coef, s)) % 11 % 10
        return check == int(s[9])
    if len(s) == 12:
        c1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        c2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        n11 = sum(c * int(d) for c, d in zip(c1, s)) % 11 % 10
        n12 = sum(c * int(d) for c, d in zip(c2, s)) % 11 % 10
        return n11 == int(s[10]) and n12 == int(s[11])
    return False


def _parse_datetime(value) -> Optional[datetime]:
    """FNS dateTime may be a unix timestamp (int) or an ISO-8601 string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        v = value.strip()
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            pass
        try:                                   # unix timestamp as a string
            return datetime.fromtimestamp(int(v), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    return None


def _operation_type(value) -> str:
    if value is None:
        return "purchase"
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return _OPERATION_TYPES.get(value, "purchase")


def _taxation_type(raw: dict) -> Optional[str]:
    value = raw.get("appliedTaxationType")
    if value is None:
        value = raw.get("taxationType")
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    for bit, name in _TAXATION_TYPES.items():   # dict insertion order = ascending bits
        if value & bit:
            return name
    return None


def _payment_form(raw: dict) -> Optional[str]:
    """Pick the payment kind whose sum > 0 (cash/card most common)."""
    for form, key in (("cash", "cashTotalSum"), ("card", "ecashTotalSum"),
                      ("prepaid", "prepaidSum"), ("credit", "creditSum")):
        val = raw.get(key)
        try:
            if val is not None and float(val) > 0:
                return form
        except (TypeError, ValueError):
            continue
    return None


def _card_last4(raw: dict) -> Optional[str]:
    """Best-effort only. The FNS check API does not expose the PAN (152-ФЗ), so
    this is None in ~99% of cases — we look only at explicit fields, never guess."""
    for key in ("cardLast4", "pan", "cardNumber"):
        v = raw.get(key)
        if isinstance(v, str) and len(v) >= 4 and v[-4:].isdigit():
            return v[-4:]
    return None


def parse_fns_response(raw_data: dict) -> dict:
    """Map an FNS receipt json into the typed receipts columns. Returns {} for a
    non-dict input. `kkt_fn` is returned for reference, but the INSERT writes the
    kkt_fn column from the dedup value (see receipts.py), not from here."""
    if not isinstance(raw_data, dict):
        return {}
    g = raw_data.get

    inn = g("userInn")
    org_inn = str(inn).strip() if validate_inn(inn) else None   # invalid INN → drop

    nds20 = g("nds20")
    nds_zero = g("ndsNo")
    return {
        "datetime":       _parse_datetime(g("dateTime")),
        "currency":       "RUB",
        "operation_type": _operation_type(g("operationType")),
        "org_legal":      _str_or_none(g("user")),
        "org_brand":      _str_or_none(g("retailPlace")),
        "org_inn":        org_inn,
        "payment_form":   _payment_form(raw_data),
        "payment_detail": _str_or_none(g("paymentDetail")),
        "card_last4":     _card_last4(raw_data),
        "tax_system":     _taxation_type(raw_data),
        "address":        _str_or_none(g("retailPlaceAddress")),
        "vat_20":         _kopecks(nds20 if nds20 is not None else g("nds18")),
        "vat_10":         _kopecks(g("nds10")),
        "vat_0":          _kopecks(nds_zero if nds_zero is not None else g("nds0")),
        "kkt_fn":         _str_or_none(g("fiscalDriveNumber")),
        "kkt_serial":     _str_or_none(g("kktNumber")),       # ЗН (заводской); часто отсутствует
        "kkt_rn":         _str_or_none(g("kktRegId")),        # РН (регистрационный)
        "fd_num":         _str_or_none(g("fiscalDocumentNumber")),
        "fpd":            _str_or_none(g("fiscalSign")),
        "cashier":        _str_or_none(g("operator")),
    }
