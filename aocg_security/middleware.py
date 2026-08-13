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

import os
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


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
    LIVENESS_EXEMPT = frozenset({"/health"})

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
        self.auth_rate_limit = (
            auth_rate_limit
            if auth_rate_limit is not None
            else _env_int("SECURITY_AUTH_RATE_LIMIT", 5)
        )
        self.ban_threshold = (
            ban_threshold
            if ban_threshold is not None
            else _env_int("SECURITY_AUTO_BAN_THRESHOLD", 10)
        )
        self.enforce_https = (
            enforce_https
            if enforce_https is not None
            else _env_bool("SECURITY_ENFORCE_HTTPS", True)
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

        # 1. Принуждение HTTPS (за прокси/Railway смотрим x-forwarded-proto).
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
