"""Parse FNS receipt line items (raw_data["items"]) into rows for receipt_items.

Same defensive contract as fns_parser: never raises on odd input, amounts in
kopecks → rubles.
"""

from typing import List

from app.parsers.fns_parser import _VAT_RATE_BY_CODE, _kopecks, _num, _str_or_none


def parse_fns_items(raw_data: dict) -> List[dict]:
    if not isinstance(raw_data, dict):
        return []
    items = raw_data.get("items")
    if not isinstance(items, list):
        return []
    result = []
    for pos, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "position": pos,
                "name": _str_or_none(item.get("name")) or "",
                "quantity": _num(item.get("quantity")),
                "price": _kopecks(item.get("price")),
                "sum": _kopecks(item.get("sum")),
                "vat_rate": _VAT_RATE_BY_CODE.get(item.get("nds")),
            }
        )
    return result


def parse_ocr_items(raw_data: dict) -> List[dict]:
    """Line items from a photo-OCR payload (raw_data["items"], shaped by ocr.py's
    _finalize_items). Differs from parse_fns_items:
      * price/sum/quantity are in RUBLES — _num, NOT _kopecks (no /100).
      * vat_rate already arrives as a string ('20'/'10'/'0'/None) — taken as-is,
        NOT mapped through _VAT_RATE_BY_CODE (which decodes FNS int nds codes).
    Same defensive contract: never raises, [] on odd input."""
    if not isinstance(raw_data, dict):
        return []
    items = raw_data.get("items")
    if not isinstance(items, list):
        return []
    result = []
    for pos, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "position": item.get("position", pos),
                "name": _str_or_none(item.get("name")) or "",
                "quantity": _num(item.get("quantity")),
                "price": _num(item.get("price")),  # RUBLES — do not /100
                "sum": _num(item.get("sum")),  # RUBLES — do not /100
                "vat_rate": _str_or_none(
                    item.get("vat_rate")
                ),  # string as-is, not a code
            }
        )
    return result
