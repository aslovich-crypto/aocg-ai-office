"""AOCGSecurityMiddleware — rate limiting + IP auto-ban + security headers +
опциональное принуждение HTTPS для FastAPI / Starlette.

Конфигурируется через переменные окружения:
    SECURITY_RATE_LIMIT        (по умолч. 60)   — запросов/минуту с одного IP
    SECURITY_AUTH_RATE_LIMIT   (по умолч. 5)    — лимит для /api/auth/* (строже)
    SECURITY_AUTO_BAN_THRESHOLD(по умолч. 10)   — сколько превышений лимита → бан IP
    SECURITY_ENFORCE_HTTPS     (по умолч. true)  — отдавать 403 на http (в dev: false)

Реализация САМОДОСТАТОЧНАЯ (in-memory скользящее окно), без внешних сервисов —
надёжно работает в одном инстансе и в pytest. Для нескольких инстансов счётчики
стоит вынести в Redis (см. README) — сигнатуры это допускают. Любой параметр
можно переопределить аргументом конструктора (имеет приоритет над env).
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


logger = logging.getLogger(__name__)

# ⚠️ ПОТОЛОК ОСЛАБЛЕНИЯ: ПАНЕЛЬ МОЖЕТ УЖЕСТОЧАТЬ, НО НЕ ОСЛАБЛЯТЬ (T80).
#
# ЗАЧЕМ, ЗАМЕРОМ. 27.08.2026 сверка панели Timeweb с кодом: в панели
# `SECURITY_AUTH_RATE_LIMIT` = 50 при умолчании кода 5. 04.09.2026 это
# подтверждено ПОВЕДЕНИЕМ прода: семь попыток входа подряд — семь раз 401
# и ни одного 429, хотя на умолчании шестая упёрлась бы в предел. То есть
# защита от перебора жила в панели, а тесты (502 зелёных) мерили умолчание
# кода — прибор смотрел не туда, где стояло значение.
#
# ПОЧЕМУ ПОТОЛОК, А НЕ ПРОСТО «ВЕРНУТЬ 5». Число в панели меняют руками,
# в спешке и без ревью; ошибка там не оставляет следа ни в git, ни в логе.
# Потолок делает ослабление НЕВОЗМОЖНЫМ: значение выше применяется как
# потолок, а расхождение уходит в журнал предупреждением.
#
# ПОЧЕМУ 20, А НЕ 5. Строгий лимит считается ПО АДРЕСУ, а сотрудники бюро
# сидят за общим офисным выходом — на всех один адрес. При 5 в минуту
# утренний вход впятером, где кто-то ошибся паролем, упёрся бы в предел,
# и защита била бы по своим. Пароль КОНКРЕТНОГО человека этим не защищён
# и без того: его держит блокировка учётной записи (5 неудач → 15 минут,
# app/routers/auth.py), которая от адреса не зависит вовсе. Строгий лимит
# нужен против перебора ПО МНОГИМ учёткам с одного адреса — там разница
# между 20 и 50 в минуту существенна, а между 20 и 5 для своих критична.
ПОТОЛОК_СТРОГОГО_ЛИМИТА = 20

# Порог автобана: сколько превышений лимита терпим до бана адреса на 5 минут.
# Большое значение обесценивает лимиты выше — превышай сколько хочешь.
ПОТОЛОК_ПОРОГА_БАНА = 20


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _не_слабее_потолка(значение: int, потолок: int, имя: str) -> int:
    """Значение из окружения, но не слабее потолка. Расхождение — в журнал.

    ⚠️ Молча применить потолок нельзя: тогда панель показывает 50, код
    работает на 20, и следующий человек будет искать причину так же долго,
    как искали мы. Предупреждение — единственный след этого расхождения.
    """
    if значение > потолок:
        logger.warning(
            "%s=%s ослабляет защиту сильнее потолка %s — применён ПОТОЛОК. "
            "Панель может ужесточать предел, но не ослаблять (T80).",
            имя,
            значение,
            потолок,
        )
        return потолок
    return значение


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# Слова, которыми защиту можно выключить ЯВНО. Всё остальное — включена.
ЯВНОЕ_НЕТ = frozenset({"0", "false", "no", "off"})


def _защита_выключена_явно(name: str) -> bool:
    """Булева настройка ЗАЩИТЫ: выключается только явным словом (S-49).

    ⚠️ ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ `_env_bool`, И ПОЧЕМУ РАЗНИЦА ВАЖНА.
    `_env_bool` считает истиной перечисленные слова, а ВСЁ ОСТАЛЬНОЕ —
    ложью. Для настройки защиты это устроено не в ту сторону: опечатка
    («tru», «True!», лишний символ) читается как «выключить» — то есть
    промах пальцем ОТКРЫВАЕТ канал, и ни ошибки, ни строки в журнале.
    Здесь наоборот: чтобы снять защиту, надо написать одно из слов
    ЯВНОГО отказа; непонятное значение оставляет её включённой и уходит
    предупреждением в журнал.
    """
    сырое = os.getenv(name)
    if сырое is None:
        return False
    значение = сырое.strip().lower()
    if значение in ЯВНОЕ_НЕТ:
        return True
    if значение not in ("1", "true", "yes", "on"):
        logger.warning(
            "%s=%r не распознано — защита ОСТАЁТСЯ включённой. "
            "Выключить можно только явно: %s (S-49).",
            name,
            сырое,
            ", ".join(sorted(ЯВНОЕ_НЕТ)),
        )
    return False


class AOCGSecurityMiddleware(BaseHTTPMiddleware):
    WINDOW_SECONDS = 60
    BAN_SECONDS = 300  # бан IP на 5 минут после превышения порога

    # Служебные /api/auth/* эндпоинты — НЕ поверхность перебора ПАРОЛЯ.
    # refresh — высокоэнтропийный машинный токен (фоновый вызов, в т.ч.
    # из нескольких вкладок); me — чтение своей сессии по валидному JWT.
    # Держим их под ОБЩИМ лимитом (rate_limit), а не строгим auth (5/мин),
    # иначе фоновые вызовы выжигают бюджет логина. login/register/
    # register-by-invite/verify-email/logout — строгие. См. S-27.
    STRICT_AUTH_EXEMPT = frozenset({"/api/auth/refresh", "/api/auth/me"})

    # Ручка состояния: её дёргает МОНИТОР площадки, а не пользователь.
    # На Timeweb App Platform проверка идёт по петле (127.0.0.1) и всегда
    # по http — заголовка `x-forwarded-proto` там нет вовсе, поэтому
    # принуждение HTTPS отдавало ей 403, площадка считала приложение
    # больным и убивала контейнер по кругу (S-06 шаг 3, 13.08.2026).
    #
    # Исключение ШИРЕ, чем только HTTPS, и это намеренно: монитор ходит
    # с ОДНОГО адреса раз в несколько секунд, а общий лимит — 60 запросов
    # в минуту с адреса. При интервале в секунду это ровно на границе,
    # а после ban_threshold превышений сработал бы автобан на 5 минут —
    # то есть та же смерть контейнера, только позже и через 429.
    #
    # Что при этом НЕ теряется: ручка не отдаёт данных, не трогает БД
    # и всегда возвращает 200 (см. app/main.py). Заголовки безопасности
    # на неё по-прежнему навешиваются. Снаружи HTTP по-прежнему закрыт —
    # обратный прокси площадки отвечает на порт 80 редиректом 308 на HTTPS.
    # ⚠️ `/health/db` ЗДЕСЬ ЖЕ, И ЭТО НЕ МЕЛОЧЬ. Она заведена 04.09.2026 для
    # второго монитора, и по ней ходит тот же прибор с одного адреса. Не будь
    # её в исключениях, повторился бы S-06 шаг 3: принуждение HTTPS отдаёт
    # 403 проверке по петле, площадка считает приложение больным и убивает
    # контейнер по кругу. Ручка данных не отдаёт и отвечает 200 либо 503.
    LIVENESS_EXEMPT = frozenset({"/health", "/health/db"})

    def __init__(
        self,
        app,
        *,
        rate_limit=None,
        auth_rate_limit=None,
        ban_threshold=None,
        enforce_https=None,
    ):
        super().__init__(app)
        self.rate_limit = (
            rate_limit
            if rate_limit is not None
            else _env_int("SECURITY_RATE_LIMIT", 60)
        )
        # ⚠️ ПОТОЛКИ ПРИМЕНЯЮТСЯ ТОЛЬКО К ОКРУЖЕНИЮ, НЕ К ЯВНОМУ АРГУМЕНТУ.
        # Аргумент передаёт ТЕСТ, и ему нужно уметь ставить любые значения —
        # в том числе заведомо слабые, чтобы проверять поведение на них.
        # Панель же не должна мочь ослабить защиту: ошибку в панели не видно
        # ни в git, ни в ревью (T80/S-49).
        self.auth_rate_limit = (
            auth_rate_limit
            if auth_rate_limit is not None
            else _не_слабее_потолка(
                _env_int("SECURITY_AUTH_RATE_LIMIT", 5),
                ПОТОЛОК_СТРОГОГО_ЛИМИТА,
                "SECURITY_AUTH_RATE_LIMIT",
            )
        )
        self.ban_threshold = (
            ban_threshold
            if ban_threshold is not None
            else _не_слабее_потолка(
                _env_int("SECURITY_AUTO_BAN_THRESHOLD", 10),
                ПОТОЛОК_ПОРОГА_БАНА,
                "SECURITY_AUTO_BAN_THRESHOLD",
            )
        )
        # ⚠️ HTTPS ВЫКЛЮЧАЕТСЯ ТОЛЬКО ЯВНЫМ «false», И ЭТО ТОЖЕ ПОТОЛОК,
        # только для булевой настройки: опечатка вроде `SECURITY_ENFORCE_HTTPS=0
        # ` с пробелом или `no` больше не открывает канал молча — она
        # прочитается как «не false», то есть защита останется включённой.
        self.enforce_https = (
            enforce_https
            if enforce_https is not None
            else not _защита_выключена_явно("SECURITY_ENFORCE_HTTPS")
        )
        self._hits: dict = defaultdict(deque)  # (ip, scope) -> очередь меток времени
        self._violations: dict = defaultdict(int)  # ip -> число превышений лимита
        self._banned: dict = {}  # ip -> время окончания бана

    @staticmethod
    def _client_ip(request: Request) -> str:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _over_limit(self, ip: str, scope: str, limit: int) -> bool:
        now = time.time()
        q = self._hits[(ip, scope)]
        cutoff = now - self.WINDOW_SECONDS
        while q and q[0] < cutoff:
            q.popleft()
        q.append(now)
        return len(q) > limit

    def _is_strict_auth(self, path: str) -> bool:
        """Путь под СТРОГИМ auth-лимитом (поверхность перебора пароля)?
        Все /api/auth/*, КРОМЕ служебных STRICT_AUTH_EXEMPT."""
        return path.startswith("/api/auth/") and path not in self.STRICT_AUTH_EXEMPT

    async def dispatch(self, request: Request, call_next):
        # 0. Проверка состояния — мимо всех ограничений (см. LIVENESS_EXEMPT).
        if request.url.path in self.LIVENESS_EXEMPT:
            return self._apply_headers(await call_next(request))

        now = time.time()
        ip = self._client_ip(request)

        # 1. Принуждение HTTPS (за прокси площадки смотрим x-forwarded-proto).
        if self.enforce_https:
            proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            if proto == "http":
                return self._apply_headers(
                    JSONResponse({"detail": "HTTPS required"}, status_code=403)
                )

        # 2. Действующий бан IP.
        ban_until = self._banned.get(ip)
        if ban_until is not None:
            if now < ban_until:
                return self._apply_headers(
                    JSONResponse({"detail": "IP temporarily banned"}, status_code=429)
                )
            del self._banned[ip]
            self._violations[ip] = 0

        # 3. Rate limit (для /api/auth/* — строже; служебные исключены — S-27).
        is_auth = self._is_strict_auth(request.url.path)
        limit = self.auth_rate_limit if is_auth else self.rate_limit
        scope = "auth" if is_auth else "general"
        if self._over_limit(ip, scope, limit):
            self._violations[ip] += 1
            if self._violations[ip] >= self.ban_threshold:
                self._banned[ip] = now + self.BAN_SECONDS
            return self._apply_headers(
                JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
            )

        response = await call_next(request)
        return self._apply_headers(response)

    def _apply_headers(self, response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        if self.enforce_https:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
