"""Parse a photo-OCR receipt payload (the dict that ocr.py's _finalize produces
and the frontend stores in raw_data) into the same typed columns as the FNS
parser.

Key differences from fns_parser (see ЧП E + audit of real prod data):
  * Amounts are already in RUBLES — do NOT divide by 100 (FNS sends kopecks).
  * operation_type / payment_form / tax_system already arrive as the data-standard
    strings ('purchase', 'card', 'osno', …) — no int→label mapping.
  * datetime is an ISO string from _finalize; we still coerce it to a datetime
    object so it fits the TIMESTAMP column (asyncpg rejects a bare string).
  * org_inn is already checksum-validated in _finalize — we don't re-validate.
  * kkt_fn is ALWAYS None for photo_ocr (Вариант A): OCR-read fiscal numbers are
    unreliable and stay only in raw_data, never in a column.

Returns the SAME 20 keys as parse_fns_response so receipts.py uses one INSERT
for both sources. Defensive: every field via .get(), never raises on bad input.
"""

from app.parsers.fns_parser import _num, _parse_datetime, _str_or_none


def parse_ocr_response(raw_data: dict) -> dict:
    """Map a finalized OCR payload into the typed receipts columns. Returns {}
    for a non-dict input. kkt_fn/kkt_serial/kkt_rn/fd_num/fpd are always None —
    photo_ocr never trusts OCR-read fiscal data (Вариант A, decided in ЧП C/E)."""
    if not isinstance(raw_data, dict):
        return {}
    g = raw_data.get
    return {
        "datetime":       _parse_datetime(g("datetime")),   # ISO str → datetime obj
        "currency":       _str_or_none(g("currency")) or "RUB",
        "operation_type": _str_or_none(g("operation_type")) or "purchase",
        "org_legal":      _str_or_none(g("org_legal")),
        "org_brand":      _str_or_none(g("org_brand")),
        "org_inn":        _str_or_none(g("org_inn")),        # already validated in _finalize
        "payment_form":   _str_or_none(g("payment_form")),
        "payment_detail": _str_or_none(g("payment_detail")),
        "card_last4":     _str_or_none(g("card_last4")),     # usually None on paper receipts
        "tax_system":     _str_or_none(g("tax_system")),
        "address":        _str_or_none(g("address")),
        "vat_20":         _num(g("vat_20")),                 # RUBLES — do not /100
        "vat_10":         _num(g("vat_10")),
        "vat_0":          _num(g("vat_0")),
        "kkt_fn":         None,                              # Вариант A — never trust OCR fn
        "kkt_serial":     None,                              # OCR does not extract fiscal reqs
        "kkt_rn":         None,
        "fd_num":         None,
        "fpd":            None,
        "cashier":        _str_or_none(g("cashier")),
    }
