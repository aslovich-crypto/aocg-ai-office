"""Переиспользуемые валидаторы (ИНН, card_last4, сумма).

Чистые функции: на валидном значении возвращают его, на некорректном бросают
ValueError. Совместимо с Pydantic field_validator (v1 `validator` и v2
`field_validator`) и пригодно standalone.

Пример с Pydantic v2:
    from pydantic import BaseModel, field_validator
    from aocg_security.validators import validate_inn

    class Org(BaseModel):
        inn: str
        _v_inn = field_validator("inn")(validate_inn)
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


def validate_inn(inn: str) -> str:
    """ИНН: 10 (юрлицо) или 12 (физлицо/ИП) цифр + контрольные суммы.
    Возвращает строку ИНН либо бросает ValueError."""
    s = str(inn).strip()
    if not re.fullmatch(r"\d{10}|\d{12}", s):
        raise ValueError("ИНН должен содержать ровно 10 или 12 цифр")
    digits = [int(c) for c in s]

    def _csum(weights):
        return sum(w * d for w, d in zip(weights, digits)) % 11 % 10

    if len(s) == 10:
        if _csum([2, 4, 10, 3, 5, 9, 4, 6, 8]) != digits[9]:
            raise ValueError("Неверная контрольная сумма ИНН (10 знаков)")
    else:  # 12
        n11 = _csum([7, 2, 4, 10, 3, 5, 9, 4, 6, 8])
        n12 = _csum([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8])
        if n11 != digits[10] or n12 != digits[11]:
            raise ValueError("Неверная контрольная сумма ИНН (12 знаков)")
    return s


def validate_card_last4(s: str) -> str:
    """Ровно 4 цифры (последние 4 карты). Возвращает строку либо ValueError."""
    v = str(s).strip()
    if not re.fullmatch(r"\d{4}", v):
        raise ValueError("card_last4 должен быть ровно 4 цифры")
    return v


def validate_amount(v: float) -> float:
    """Сумма > 0 и не более 2 знаков после точки. Возвращает float либо ValueError."""
    try:
        dec = Decimal(str(v))
    except (InvalidOperation, ValueError) as e:
        raise ValueError("Сумма должна быть числом") from e
    if dec <= 0:
        raise ValueError("Сумма должна быть больше 0")
    if dec.as_tuple().exponent < -2:
        raise ValueError("Сумма: не более 2 знаков после точки")
    return float(v)
