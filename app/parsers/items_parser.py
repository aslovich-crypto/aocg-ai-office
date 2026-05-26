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
        result.append({
            "position": pos,
            "name":     _str_or_none(item.get("name")) or "",
            "quantity": _num(item.get("quantity")),
            "price":    _kopecks(item.get("price")),
            "sum":      _kopecks(item.get("sum")),
            "vat_rate": _VAT_RATE_BY_CODE.get(item.get("nds")),
        })
    return result
