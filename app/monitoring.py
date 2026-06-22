"""Мониторинг ошибок через Sentry + scrub ПД в before_send.

Sentry включается ТОЛЬКО при заданном SENTRY_DSN (как RESEND_API_KEY):
пустой DSN → init не вызывается, приложение стартует без Sentry (локалка/CI).
Захват только ошибок: traces_sample_rate=0, sample_rate=1.0,
send_default_pii=False. Перед отправкой событие проходит _sentry_scrub —
маскирование ПД переиспользует mask_log_dict из aocg_security (не дублируем).
"""

from __future__ import annotations

import logging
import os
import re

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from starlette.requests import Request
from starlette.responses import JSONResponse

from aocg_security.masking import mask_log_dict

logger = logging.getLogger(__name__)

# Имена query-параметров, значения которых нельзя слать в Sentry. mask_log_dict
# работает по КЛЮЧАМ dict, но НЕ по свободному тексту url/query_string — поэтому
# чувствительные значения в query вырезаем регуляркой здесь.
_SENSITIVE_QS = re.compile(
    r"(?i)\b(inn|inns|userinn|org_inn|fn|kkt_fn|token|tokens|access_token|"
    r"refresh_token|secret|api_?key|password|card|card_number|snils|passport)"
    r"=([^&\s]*)"
)


def _scrub_query(value: str) -> str:
    """Заменяет значения чувствительных query-параметров на ***."""
    return _SENSITIVE_QS.sub(lambda m: f"{m.group(1)}=***", value)


def _sentry_scrub(event, hint):
    """before_send: маскирует ПД до отправки. При любой ошибке scrub'а → None
    (НЕ слать неотскрабленное событие — приоритет защиты ПД над полнотой)."""
    try:
        extra = event.get("extra")
        if isinstance(extra, dict):
            event["extra"] = mask_log_dict(extra)

        req = event.get("request")
        if isinstance(req, dict):
            data = req.get("data")
            if isinstance(data, dict):
                req["data"] = mask_log_dict(data)
            qs = req.get("query_string")
            if isinstance(qs, str):
                req["query_string"] = _scrub_query(qs)
            url = req.get("url")
            if isinstance(url, str):
                req["url"] = _scrub_query(url)
        return event
    except Exception:  # noqa: BLE001 — scrub не должен пропустить сырые ПД
        logger.warning("sentry before_send scrub failed — событие отброшено")
        return None


def init_sentry() -> bool:
    """Инициализирует Sentry, если задан SENTRY_DSN. True — init выполнен;
    False — DSN пуст (Sentry выключен, как при пустом RESEND_API_KEY)."""
    dsn = os.getenv("SENTRY_DSN", "")
    if not dsn:
        return False
    sentry_sdk.init(
        dsn=dsn,
        traces_sample_rate=0,
        sample_rate=1.0,
        send_default_pii=False,
        environment=os.getenv("ENVIRONMENT", "production"),
        before_send=_sentry_scrub,
        integrations=[FastApiIntegration()],
    )
    return True


async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all для НЕобработанных исключений: структурный лог + generic-JSON
    без traceback. HTTPException / RateLimitExceeded сюда НЕ попадают (у них
    свои хендлеры — Starlette диспетчеризует по типу). Sentry ловит исключение
    через FastApiIntegration ДО этого хендлера, событие не теряется."""
    logger.error(
        "Unhandled exception on %s %s",
        request.method,
        request.url.path,  # только path, без query — без ПД в логе
        exc_info=True,
    )
    return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка"})
