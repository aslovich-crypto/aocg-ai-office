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
    1: "purchase",
    2: "refund",
    3: "expense",
    4: "expense_refund",
    "Приход": "purchase",
    "Возврат прихода": "refund",
    "Расход": "expense",
    "Возврат расхода": "expense_refund",
}

# taxationType / appliedTaxationType: FNS tag 1055, a bitmask. Lowest set bit wins.
_TAXATION_TYPES = {
    1: "osno",
    2: "usn_income",
    4: "usn_income_minus_expense",
    8: "envd",
    16: "eshn",
    32: "psn",
    64: "npd",
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
        try:  # unix timestamp as a string
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
    for bit, name in _TAXATION_TYPES.items():  # dict insertion order = ascending bits
        if value & bit:
            return name
    return None


def _payment_form(raw: dict) -> Optional[str]:
    """Pick the payment kind whose sum > 0 (cash/card most common)."""
    for form, key in (
        ("cash", "cashTotalSum"),
        ("card", "ecashTotalSum"),
        ("prepaid", "prepaidSum"),
        ("credit", "creditSum"),
    ):
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


# Тег 1199 ФФД: код ставки → процент. Расчётные (3/4/9/10/12) = та же ставка, что прямые.
VAT_RATE = {1: 20, 2: 10, 3: 20, 4: 10, 5: 0, 7: 5, 8: 7, 9: 5, 10: 7, 11: 22, 12: 22}


def _vat_breakdown(items) -> Optional[dict]:
    """{ставка_str: сумма_НДС_рубли} по позициям (items[].nds + ndsSum). Top-level
    nds* ФНС шлёт не для всех ставок → считаем из позиций. 0%/без НДС не кладём."""
    acc: dict = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        rate = VAT_RATE.get(it.get("nds"))
        if rate:  # >0
            ns = it.get("ndsSum")
            if ns:
                acc[str(rate)] = acc.get(str(rate), 0) + ns
    return {k: round(v / 100, 2) for k, v in acc.items()} or None


def _breakdown_из_верхних(g) -> Optional[dict]:
    """Разбивка из ВЕРХНЕУРОВНЕВЫХ полей ФНС, когда позиций с кодами ставок нет.

    ЗАЧЕМ. `_vat_breakdown` считает по `items[].nds`, но ФНС присылает и
    верхнеуровневые `nds20`/`nds18`/`nds10` — и бывают ответы, где позиции
    без кодов, а суммы наверху есть. До NDS-CLEANUP ② это не мешало: суммы
    ложились в колонки `vat_20`/`vat_10`. После того как колонки убраны,
    такой чек остался бы БЕЗ НДС вовсе — молча, потому что «пустая разбивка»
    и «НДС нет» выглядят одинаково.

    Ловушку назвал владелец до реализации; она оказалась не гипотетической —
    ровно такой ответ лежит в фикстуре `tests/test_fns_parser.py`.

    `nds18` — legacy-ставка 18%, ФНС отдаёт её в старых чеках; кладём как 20,
    как и делал прежний код (`vat_20` брал nds18 при отсутствии nds20).
    """
    из_верхних = {}
    for поле, ставка in (("nds20", "20"), ("nds18", "20"), ("nds10", "10")):
        сумма = _kopecks(g(поле))
        if сумма:
            из_верхних[ставка] = round(из_верхних.get(ставка, 0) + сумма, 2)
    return из_верхних or None


def parse_fns_response(raw_data: dict) -> dict:
    """Map an FNS receipt json into the typed receipts columns. Returns {} for a
    non-dict input. `kkt_fn` is returned for reference, but the INSERT writes the
    kkt_fn column from the dedup value (see receipts.py), not from here."""
    if not isinstance(raw_data, dict):
        return {}
    g = raw_data.get

    inn = g("userInn")
    org_inn = str(inn).strip() if validate_inn(inn) else None  # invalid INN → drop

    # nds20/nds18/nds10 читает теперь _breakdown_из_верхних — отдельные
    # переменные под них не нужны (NDS-CLEANUP ②).
    nds_zero = g("ndsNo")
    return {
        "datetime": _parse_datetime(g("dateTime")),
        "currency": "RUB",
        "operation_type": _operation_type(g("operationType")),
        "org_legal": _str_or_none(g("user")),
        "org_brand": _str_or_none(g("retailPlace")),
        "org_inn": org_inn,
        "payment_form": _payment_form(raw_data),
        "payment_detail": _str_or_none(g("paymentDetail")),
        "card_last4": _card_last4(raw_data),
        "tax_system": _taxation_type(raw_data),
        "address": _str_or_none(g("retailPlaceAddress")),
        # ⚠️ vat_0 — ПРЕЖНЕЕ ПОВЕДЕНИЕ СОХРАНЕНО ДОСЛОВНО, включая дефект.
        # Трогать её здесь нельзя: показ карточки ещё читает r.vat_0
        # (ReceiptDetailModal, пометка «Без НДС»). Переключение показа —
        # отдельный коммит (№28), после него колонка становится мёртвой
        # и снимается замером, как снимали vat_20/vat_10 в NDS-CLEANUP.
        "vat_0": _kopecks(nds_zero if nds_zero is not None else g("nds0")),
        # ДВЕ ВЕЛИЧИНЫ, КАЖДАЯ ИЗ СВОЕГО ТЕГА И НЕЗАВИСИМО ОТ ДРУГОЙ.
        # Никаких «если одно, то другое»: на чеке id=61 обе стороны
        # присутствуют одновременно, и любая связка между ними снова
        # потеряла бы одну из них.
        "sum_vat_0": _kopecks(g("nds0")),  # тег 1104 — оборот по ставке 0%
        "sum_no_vat": _kopecks(nds_zero),  # тег 1105 — оборот без НДС
        # Сначала по позициям (полная, по тегу 1199), при пустом результате —
        # из верхнеуровневых полей. НДС не должен исчезать оттого, что
        # в позициях не оказалось кодов ставок.
        "vat_breakdown": _vat_breakdown(g("items")) or _breakdown_из_верхних(g),
        # NDS-CLEANUP ②: vat_20/vat_10 больше не отдаются — они были списком
        # из двух ставок, и на ставке 22% этот список занизил входящий НДС
        # на 70,5% (NDS-VAT22). У ФНС разбивка полная, по тегу 1199.
        # vat_total — «НДС есть, ставка не распознана» — нужен ТОЛЬКО
        # фото-пути; здесь None, и ключ присутствует ради контракта
        # keys_match: один INSERT обслуживает оба парсера, и разъехавшиеся
        # множества ключей записали бы в базу NULL молча.
        "vat_total": None,
        "kkt_fn": _str_or_none(g("fiscalDriveNumber")),
        "kkt_serial": _str_or_none(g("kktNumber")),  # ЗН (заводской); часто отсутствует
        "kkt_rn": _str_or_none(g("kktRegId")),  # РН (регистрационный)
        "fd_num": _str_or_none(g("fiscalDocumentNumber")),
        "fpd": _str_or_none(g("fiscalSign")),
        "cashier": _str_or_none(g("operator")),
    }
