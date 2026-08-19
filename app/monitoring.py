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

from aocg_security.masking import _SECRET_KEYS, mask_log_dict

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


# ─── Разрешительный список для request.data (S-66, S-68, S-69) ───────────────
# ПРИНЦИП ЗДЕСЬ ОБРАТНЫЙ mask_log_dict, И ЭТО НАМЕРЕННО. mask_log_dict режет
# ИЗВЕСТНОЕ ПЛОХОЕ (список ключей ПД); он подвёл трижды — на `text` шлюза MAX,
# на 16 полях с именами в шести роутерах и внутри `raw_data`, где ключи
# придумали не мы, а ФНС. Здесь наоборот: остаётся только ЯВНО НАЗВАННОЕ
# безопасное, остальное режется. Отказ становится шумным (в разборе не хватит
# поля) вместо тихого (ПД ушли и никто не заметил).
#
# Область — ТОЛЬКО request.data: там лежит присланное пользователем, а в этом
# приложении присланное пользователем и есть ПД. event["extra"] наполняет наш
# собственный код, свободного пользовательского текста там нет, и он остаётся
# на mask_log_dict.
#
# Список собран замером по ВСЕМ моделям запросов проекта (48 полей), а не по
# памяти. Каждое имя — идентификатор, перечисление или флаг: ни одно не
# приходит свободным текстом. `region` в список НЕ вошёл: по таблице он похож
# на перечисление, а по модели (`users.py:110`) — Optional[str].
_DATA_ALLOWLIST = frozenset(
    {
        # идентификаторы и ссылки: значение бессмысленно без базы
        "chat_id",
        "employee_id",
        "employee_number",
        "group_id",
        "ids",
        "receiptids",
        "photo_key",
        # перечисления: конечный набор значений, задан нашим кодом
        "role",
        "source",
        "status",
        "org_type",
        "tax_kind",
        "tax_system",
        "format",
        # флаги и числа настройки
        "force",
        "is_visible",
        "notify",
        "max_uses",
        "expires_hours",
    }
)

# ⚠️ ЗАВИСИМОСТЬ ОТ ЗАПРЕТИТЕЛЬНОГО МОДУЛЯ НАМЕРЕННАЯ: для ключей из
# _SECRET_KEYS форма метки ДРУГАЯ — без типа и длины. Длина пароля сужает
# перебор, и то же верно для token, refresh_token, api_key.
# ЕСЛИ СЮДА ДОБАВЯТ КЛЮЧ, ПОВЕДЕНИЕ ЭТОЙ ФУНКЦИИ ИЗМЕНИТСЯ МОЛЧА.
_MAX_DEPTH = 6


def _метка(значение) -> str:
    """Форма вырезанного: тип и размер, но НЕ содержимое."""
    if isinstance(значение, dict):
        return f"<вырезано: dict, {len(значение)} ключей>"
    if isinstance(значение, (list, tuple)):
        return f"<вырезано: list, {len(значение)} элементов>"
    if isinstance(значение, str):
        return f"<вырезано: str, {len(значение)}>"
    return f"<вырезано: {type(значение).__name__}>"


def _простое(значение) -> bool:
    """Скаляр или список скаляров — то, ради чего ключ попал в список."""
    if значение is None or isinstance(значение, (str, int, float, bool)):
        return True
    if isinstance(значение, (list, tuple)):
        return all(
            x is None or isinstance(x, (str, int, float, bool)) for x in значение
        )
    return False


def _scrub_request_data(значение, глубина: int = 0):
    """Разрешительная фильтрация request.data. Вариант «б»: неразрешённый узел
    режется ЦЕЛИКОМ, внутрь не спускаемся.

    Почему не спускаемся: список собран по НАШИМ моделям верхнего уровня.
    На произвольной глубине то же имя несёт другое содержимое — `format`
    наверху это "markdown" из закрытого набора, а внутри чужого объекта
    может быть свободным текстом.
    """
    if глубина > _MAX_DEPTH:
        return "<вырезано: слишком глубоко>"
    if isinstance(значение, dict):
        итог = {}
        for k, v in значение.items():
            kl = str(k).lower()
            if kl in _SECRET_KEYS:
                итог[k] = "<вырезано>"
            elif kl in _DATA_ALLOWLIST and _простое(v):
                итог[k] = v
            else:
                итог[k] = _метка(v)
        return итог
    if isinstance(значение, (list, tuple)):
        return [_scrub_request_data(x, глубина + 1) for x in значение]
    # ⚠️ ХВОСТ РЕЖЕТ, А НЕ ПРОПУСКАЕТ. Сюда попадает то, у чего НЕТ ключа:
    # сырое тело запроса строкой на верхнем уровне и скаляры внутри списков.
    # Разрешение выдаётся ИМЕНИ КЛЮЧА; нет ключа — нет и разрешения.
    # Найдено вопросом владельца 19.08.2026: до правки строка с именем и ИНН
    # проходила насквозь, и это было бы дырой ШИРЕ исходной — ни один ключ
    # такое тело не защищает.
    return _метка(значение)


def _sentry_scrub(event, hint):
    """before_send: маскирует ПД до отправки. При любой ошибке scrub'а → None
    (НЕ слать неотскрабленное событие — приоритет защиты ПД над полнотой)."""
    try:
        extra = event.get("extra")
        if isinstance(extra, dict):
            event["extra"] = mask_log_dict(extra)

        req = event.get("request")
        if isinstance(req, dict):
            # S-68/S-69: разрешительный список вместо mask_log_dict.
            # Меняется ТОЛЬКО эта ветка; query_string, url и extra прежние.
            if "data" in req:
                req["data"] = _scrub_request_data(req["data"])
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
