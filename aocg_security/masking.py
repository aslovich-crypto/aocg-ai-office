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
_INN_KEYS = {"inn", "inns", "userinn", "org_inn"}
# fiscaldrivenumber=ФН, fiscaldocumentnumber=ФД, fiscalsign=ФПД — все фискальные
# номера маскируются как ФН (0.1.1: добавлены ФД/ФПД из ответа ФНС).
_FN_KEYS = {"fn", "kkt_fn", "fiscaldrivenumber", "fiscaldocumentnumber", "fiscalsign"}
_CARD_KEYS = {"card_last4", "card_number", "card"}
# Полное скрытие ('***'): пароли, токены, ключи доступа, паспорт, СНИЛС.
_SECRET_KEYS = {
    "password",
    "password_hash",
    "old_password",
    "new_password",
    "token",
    "tokens",
    "access_token",
    "refresh_token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "passport",
    "snils",
}
_OPERATOR_KEYS = {"operator", "cashier"}  # кассир в ответе ФНС и parsed-алиас


def mask_inn(inn) -> str:
    """7707083893 → '****3893'. Видны только последние 4 цифры — по реестрам
    ФНС открытые код региона+ИФНС позволяли сузить идентификацию, поэтому
    префикс скрыт (152-ФЗ). Пустой ввод → '', слишком короткий (<6) → '***'."""
    s = re.sub(r"\s", "", str(inn or ""))
    if not s:
        return ""
    if len(s) < 6:
        return "***"
    return f"****{s[-4:]}"


def mask_card(card) -> str:
    """Номер карты или card_last4 → '****1234' (видны последние 4 цифры)."""
    digits = re.sub(r"\D", "", str(card or ""))
    if len(digits) < 4:
        return "****"
    return f"****{digits[-4:]}"


def mask_fn(fn) -> str:
    """ФН полностью скрывается (фискальный накопитель, юр. значимость)."""
    return "[fn:скрыт]" if str(fn or "").strip() else ""


def _mask_scalar(kl, v):
    """Маскирует одиночное значение по классу ключа (kl — ключ в lower-case).
    Нечувствительный ключ → значение возвращается без изменений."""
    if kl in _INN_KEYS:
        return mask_inn(v)
    if kl in _FN_KEYS:
        return mask_fn(v)
    if kl in _CARD_KEYS:
        return mask_card(v)
    if kl in _OPERATOR_KEYS:
        return "[скрыт]"
    if kl in _SECRET_KEYS:
        return "***"
    return v


def mask_log_dict(d):
    """Рекурсивно возвращает КОПИЮ словаря с маскированными чувствительными
    полями. Не-dict значения отдаются как есть; вложенные dict/list тоже
    обходятся (например, raw_data чека). Элементы списка под чувствительным
    ключом (inns: [...], tokens: [...]) маскируются поэлементно. Исходный
    словарь не мутируется."""
    if not isinstance(d, dict):
        return d
    out = {}
    for k, v in d.items():
        kl = str(k).lower()
        if isinstance(v, dict):
            out[k] = mask_log_dict(v)
        elif isinstance(v, list):
            out[k] = [
                mask_log_dict(x) if isinstance(x, dict) else _mask_scalar(kl, x)
                for x in v
            ]
        else:
            out[k] = _mask_scalar(kl, v)
    return out
