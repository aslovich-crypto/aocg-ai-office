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

    def __init__(self, app, *, rate_limit=None, auth_rate_limit=None,
                 ban_threshold=None, enforce_https=None):
        super().__init__(app)
        self.rate_limit = rate_limit if rate_limit is not None else _env_int("SECURITY_RATE_LIMIT", 60)
        self.auth_rate_limit = auth_rate_limit if auth_rate_limit is not None else _env_int("SECURITY_AUTH_RATE_LIMIT", 5)
        self.ban_threshold = ban_threshold if ban_threshold is not None else _env_int("SECURITY_AUTO_BAN_THRESHOLD", 10)
        self.enforce_https = enforce_https if enforce_https is not None else _env_bool("SECURITY_ENFORCE_HTTPS", True)
        self._hits: dict = defaultdict(deque)   # (ip, scope) -> очередь меток времени
        self._violations: dict = defaultdict(int)  # ip -> число превышений лимита
        self._banned: dict = {}                  # ip -> время окончания бана

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

    async def dispatch(self, request: Request, call_next):
        now = time.time()
        ip = self._client_ip(request)

        # 1. Принуждение HTTPS (за прокси/Railway смотрим x-forwarded-proto).
        if self.enforce_https:
            proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            if proto == "http":
                return self._apply_headers(
                    JSONResponse({"detail": "HTTPS required"}, status_code=403))

        # 2. Действующий бан IP.
        ban_until = self._banned.get(ip)
        if ban_until is not None:
            if now < ban_until:
                return self._apply_headers(
                    JSONResponse({"detail": "IP temporarily banned"}, status_code=429))
            del self._banned[ip]
            self._violations[ip] = 0

        # 3. Rate limit (для /api/auth/* — строже).
        is_auth = request.url.path.startswith("/api/auth/")
        limit = self.auth_rate_limit if is_auth else self.rate_limit
        scope = "auth" if is_auth else "general"
        if self._over_limit(ip, scope, limit):
            self._violations[ip] += 1
            if self._violations[ip] >= self.ban_threshold:
                self._banned[ip] = now + self.BAN_SECONDS
            return self._apply_headers(
                JSONResponse({"detail": "Rate limit exceeded"}, status_code=429))

        response = await call_next(request)
        return self._apply_headers(response)

    def _apply_headers(self, response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if self.enforce_https:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response
