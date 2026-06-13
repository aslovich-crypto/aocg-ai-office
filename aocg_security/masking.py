"""Маскирование чувствительных полей (ИНН, карты, ФН, пароли) в логах.

152-ФЗ: фискальные и платёжные реквизиты не должны попадать в логи открытым
текстом. Все функции чистые (без побочных эффектов) — годятся и для
logging-фильтра, и для ручного masked-вывода.
"""

from __future__ import annotations

import re

# Ключи словаря, которые маскируются в mask_log_dict (регистронезависимо).
# Базовый набор из спецификации + практичные алиасы под схему aocg-ai-office
# (org_inn / userInn / kkt_fn / fiscalDriveNumber реально встречаются в raw_data).
_INN_KEYS = {"inn", "userinn", "org_inn"}
# fiscaldrivenumber=ФН, fiscaldocumentnumber=ФД, fiscalsign=ФПД — все фискальные
# номера маскируются как ФН (0.1.1: добавлены ФД/ФПД из ответа ФНС).
_FN_KEYS = {"fn", "kkt_fn", "fiscaldrivenumber", "fiscaldocumentnumber", "fiscalsign"}
_CARD_KEYS = {"card_last4", "card_number", "card"}
_SECRET_KEYS = {"password", "password_hash", "old_password", "new_password"}
_OPERATOR_KEYS = {"operator"}  # кассир в ответе ФНС (parsed-алиас "cashier" — Фаза 2)


def mask_inn(inn) -> str:
    """7707083893 → '770****93'. Пустой ввод → '', слишком короткий → '***'."""
    s = re.sub(r"\s", "", str(inn or ""))
    if not s:
        return ""
    if len(s) < 6:
        return "***"
    return f"{s[:3]}****{s[-2:]}"


def mask_card(card) -> str:
    """Номер карты или card_last4 → '****1234' (видны последние 4 цифры)."""
    digits = re.sub(r"\D", "", str(card or ""))
    if len(digits) < 4:
        return "****"
    return f"****{digits[-4:]}"


def mask_fn(fn) -> str:
    """ФН полностью скрывается (фискальный накопитель, юр. значимость)."""
    return "[fn:скрыт]" if str(fn or "").strip() else ""


def mask_log_dict(d):
    """Рекурсивно возвращает КОПИЮ словаря с маскированными чувствительными
    полями. Не-dict значения отдаются как есть; вложенные dict/list тоже
    обходятся (например, raw_data чека). Исходный словарь не мутируется."""
    if not isinstance(d, dict):
        return d
    out = {}
    for k, v in d.items():
        kl = str(k).lower()
        if isinstance(v, dict):
            out[k] = mask_log_dict(v)
        elif isinstance(v, list):
            out[k] = [mask_log_dict(x) if isinstance(x, dict) else x for x in v]
        elif kl in _INN_KEYS:
            out[k] = mask_inn(v)
        elif kl in _FN_KEYS:
            out[k] = mask_fn(v)
        elif kl in _CARD_KEYS:
            out[k] = mask_card(v)
        elif kl in _OPERATOR_KEYS:
            out[k] = "[скрыт]"
        elif kl in _SECRET_KEYS:
            out[k] = "***"
        else:
            out[k] = v
    return out
