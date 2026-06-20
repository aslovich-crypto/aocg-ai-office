"""aocg-security — переиспользуемые security-компоненты АОЦГ (Фаза 1).

Публичный API: маскирование ПД, валидаторы, security-middleware.
"""

from aocg_security.masking import mask_inn, mask_card, mask_fn, mask_log_dict
from aocg_security.validators import (
    validate_inn,
    validate_card_last4,
    validate_amount,
)
from aocg_security.middleware import AOCGSecurityMiddleware

__version__ = "0.1.2"
__all__ = [
    "mask_inn",
    "mask_card",
    "mask_fn",
    "mask_log_dict",
    "validate_inn",
    "validate_card_last4",
    "validate_amount",
    "AOCGSecurityMiddleware",
]
