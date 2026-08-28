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

# ═══ ТЕГ 1199 ФФД — ЕДИНСТВЕННАЯ КАРТА КОДОВ СТАВОК В БЭКЕНДЕ ═══
#
# ⚠️ ДО 28.08.2026 ИХ БЫЛО ДВЕ, И ОБЕ НЕВЕРНЫЕ ПО-СВОЕМУ (№28, родня T39):
#   `_VAT_RATE_BY_CODE` — шесть кодов; писала в `receipt_items.vat_rate`.
#       Коды 7–12 (5%, 7%, 22% и расчётные) отсутствовали → NULL.
#       Коды 5 и 6 схлопнуты в "0" → «НДС 0%» неотличим от «Без НДС».
#       Коды 3 и 4 отдавали "20"/"10" → расчётная ставка = прямой.
#   `VAT_RATE` — двенадцать кодов, но код 6 не знала вовсе, и значения
#       были числами, где 20/120 неотличима от 20.
#
# ЗАМЕР 28.08.2026 НА ПРОДЕ, 595 позиций — цена раздвоения:
#   код 6  → 448 позиций (три четверти всех!) записаны как "0"
#   код 11 → 80 позиций записаны как NULL — ровно все 80 без ставки
#   код 2  → 51, код 5 → 7, без поля `nds` → 9 (это photo_ocr, S-45)
# Совпадение «80 с кодом 11» и «80 без ставки» точное: третьей причины нет.
#
# ⚠️ ЗНАЧЕНИЯ — СТРОКИ, И РАСЧЁТНАЯ СТАВКА ОТЛИЧАЕТСЯ ОТ ПРЯМОЙ.
# 20 и 20/120 дают одинаковый процент, но разную базу: 20 начисляется
# СВЕРХ цены, 20/120 выделяется ИЗ неё. Свести их — потерять различие,
# которое бухгалтер обязан видеть в первичном документе.
# Код 6 получает "без НДС", а НЕ NULL: NULL уже занят под «данных нет»
# (девять позиций photo_ocr), и смешать эти два состояния значит вернуть
# ровно ту ошибку, ради которой заведена вся строка №28.
СТАВКА_ПО_КОДУ = {
    1: "20",
    2: "10",
    3: "20/120",
    4: "10/110",
    5: "0",
    6: "без НДС",
    7: "5",
    8: "7",
    9: "5/105",
    10: "7/107",
    11: "22",
    12: "22/122",
}

# ⚠️ КОДЫ, ПО КОТОРЫМ НАЛОГ ЕСТЬ — отдельно от карты, а не «ставка > 0».
# Код 5 (ставка 0%) и код 6 (без НДС) оба дают нулевую сумму налога, но
# по РАЗНЫМ причинам, и обороты по ним живут в своих колонках
# (`sum_vat_0` / `sum_no_vat`, №30). В разбивку сумм НДС не попадает ни тот,
# ни другой — там были бы нули, неотличимые от отсутствия строки.
КОДЫ_С_НАЛОГОМ = frozenset({1, 2, 3, 4, 7, 8, 9, 10, 11, 12})


def _код_ставки(value) -> Optional[int]:
    """Код тега 1199 из int или числовой строки, иначе None.

    ФНС шлёт int, но провайдеры проверки чека echo-ят и строку. Модуль
    объявлен defensive by design и уже принимает оба вида в `_OPERATION_TYPES`;
    здесь цена ошибки та же — молчаливый NULL в колонке ставки.
    """
    if isinstance(value, bool):  # bool — подкласс int, но кодом ставки не бывает
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


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


def _vat_breakdown(items) -> Optional[dict]:
    """{ставка_str: сумма_НДС_рубли} по позициям (items[].nds + ndsSum).

    ВТОРОЙ источник свода из трёх (см. `parse_fns_response`). Ставки 0%
    и «без НДС» сюда не кладём — там нули, а нулевая строка в разбивке
    неотличима от отсутствия строки."""
    acc: dict = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        код = _код_ставки(it.get("nds"))
        if код not in КОДЫ_С_НАЛОГОМ:
            continue
        ns = it.get("ndsSum")
        if ns:
            ставка = СТАВКА_ПО_КОДУ[код]
            acc[ставка] = acc.get(ставка, 0) + ns
    return {k: round(v / 100, 2) for k, v in acc.items()} or None


def _оборот_по_ставке_ноль(g) -> Optional[float]:
    """Тег 1104 — но НЕ служебная заглушка.

    ⚠️ ПРАВИЛО ФОРМАТА, приказ ФНС ЕД-7-20/662@ (ред. 26.03.2025), табл. 11.2:
    если сведения о суммах НДС есть ТОЛЬКО в составе тега 1115 (контейнер),
    в чек включается тег 1104 СО ЗНАЧЕНИЕМ НОЛЬ, и он **не включается
    в печатную форму**. Это служебная заглушка формата, а не оборот.

    ⚠️ ПОВОД, ЗАМЕР 28.08.2026: заглушек шесть, и после выката №30 все шесть
    ПОКАЗЫВАЛИСЬ на карточке строкой «Оборот по ставке 0% — 0,00 ₽». Прежняя
    пометка их прятала случайно (её гасил код 22% в позициях), а явный показ
    оборотов — нет. Печатать величину, которую формат печатать запрещает,
    хуже, чем не печатать ничего: ноль читается как «проверено, оборота нет».

    Возвращаем None — «не знаем», и это ПРАВДА: собственного значения
    у тега здесь нет, есть только контейнер.
    """
    значение = _kopecks(g("nds0"))
    if значение == 0 and isinstance(g("amountsReceiptNds"), dict):
        return None
    return значение


def _breakdown_из_контейнера(g) -> Optional[dict]:
    """ПЕРВЫЙ и главный источник свода: тег 1115 «суммы НДС чека».

    ⚠️ ДО 28.08.2026 ЭТО ПОЛЕ НЕ ЧИТАЛ НИКТО — грепом по обоим репозиториям.
    Замер 28.08: контейнер есть у 17 чеков из 71, свод потерян у 3 (две недели
    назад было 2 — дефект НАКАПЛИВАЕТСЯ). Остальные 14 показывали разбивку
    ПО СЛУЧАЙНОСТИ: у них позиции несли и код, и `ndsSum`, и свод сложился
    из позиций, а контейнер не участвовал ни разу.

    ⚠️ ПОЧЕМУ ПЕРВЫЙ, А НЕ ЗАПАСНОЙ. Это СВОД САМОЙ ФНС, а не наш расчёт по
    позициям. Приказ ЕД-7-20/662@ (ред. 26.03.2025), табл. 11.2: 1115 —
    контейнер, внутри «сумма НДС чека» (тег 1119) ОДНОКРАТНО НА КАЖДУЮ СТАВКУ,
    ставка задаётся тегом 1199. Ровно структура `amountsReceiptNds.amountsNds`.

    ⚠️ И СЛЕДСТВИЕ, КОТОРОЕ ВАЖНЕЕ САМОГО ПОЛЯ: плоских тегов под НОВЫЕ ставки
    (5%, 7%, 22% и расчётные) в наборе 1102–1107 НЕТ. Новая ставка физически
    не может приехать иначе как контейнером. Не читая его, мы не видели НИ ОДНУ
    новую ставку структурно, а не по случайности — и замер это подтвердил:
    во всех 17 чеках с контейнером код внутри только 11, то есть 22%.
    """
    контейнер = g("amountsReceiptNds")
    if not isinstance(контейнер, dict):
        return None
    acc: dict = {}
    for запись in контейнер.get("amountsNds") or []:
        if not isinstance(запись, dict):
            continue
        код = _код_ставки(запись.get("nds"))
        if код not in КОДЫ_С_НАЛОГОМ:
            continue
        сумма = запись.get("ndsSum")
        if сумма:
            ставка = СТАВКА_ПО_КОДУ[код]
            acc[ставка] = acc.get(ставка, 0) + сумма
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
    for поле, ставка in (
        ("nds20", "20"),
        ("nds18", "20"),
        ("nds10", "10"),
        # РАСЧЁТНЫЕ СТАВКИ, теги 1106 и 1107 — до 28.08.2026 не читал никто.
        # У каждой по два имени в ответах проверки чека: короткое (ndsXXYYY)
        # и длинное (ndsCalculatedXX). Складываем, как уже сложены nds20+nds18:
        # оба имени одной величины в одном ответе не встречаются, а если
        # встретятся — сумма честнее молчаливого выбора одного из них.
        ("nds18118", "20/120"),
        ("ndsCalculated20", "20/120"),
        ("nds10110", "10/110"),
        ("ndsCalculated10", "10/110"),
    ):
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
        "sum_vat_0": _оборот_по_ставке_ноль(g),  # тег 1104
        "sum_no_vat": _kopecks(nds_zero),  # тег 1105 — оборот без НДС
        # ═══ КАНОН СВОДА НДС: ТРИ ИСТОЧНИКА, СТРОГИЙ ПРИОРИТЕТ (№28 ②) ═══
        #
        # ① КОНТЕЙНЕР 1115 — свод самой ФНС. Единственный, куда приходят
        #    новые ставки (5%, 7%, 22%): плоских тегов под них в формате нет.
        # ② ПОЗИЦИИ (тег 1199 + ndsSum) — НАШ расчёт, но по данным чека.
        # ③ ПЛОСКИЕ ТЕГИ 1102–1107 — старое поколение, включая расчётные.
        #
        # ⚠️ ПОЧЕМУ ИМЕННО ТАКОЙ ПОРЯДОК, А НЕ «что нашлось». До 28.08.2026
        # источников было ДВА (② и ③), и свод терялся у 3 чеков из 17:
        # позиции несли код ставки БЕЗ суммы, ② давала пусто, ③ про 22% не
        # знает — на карточке не рисовалось НИЧЕГО при наличии данных от ФНС.
        # Замер две недели назад давал 2 чека, теперь 3: дефект накапливается.
        #
        # ⚠️ ЧТО ЭТОТ ПОРЯДОК МЕНЯЕТ ДЛЯ УЖЕ РАЗОБРАННЫХ ЧЕКОВ. У 14 чеков
        # свод сегодня сложен из позиций, а контейнер у них ТОЖЕ есть. Теперь
        # победит контейнер. Если он и позиции разойдутся — это находка, а не
        # мелочь, и сухой прогон пересчёта (③) обязан на ней ОСТАНОВИТЬСЯ.
        "vat_breakdown": (
            _breakdown_из_контейнера(g)
            or _vat_breakdown(g("items"))
            or _breakdown_из_верхних(g)
        ),
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
