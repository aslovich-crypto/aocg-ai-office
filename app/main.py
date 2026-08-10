import logging
import os
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.database import init_db
from app.routers import (
    receipts,
    reports,
    fns,
    cards,
    ocr,
    consent,
    users,
    services,
    auth,
    categories,
    organizations,
)
from aocg_security.middleware import AOCGSecurityMiddleware
from app.monitoring import init_sentry, unhandled_exception_handler

# ─── ЖУРНАЛ ПРИЛОЖЕНИЯ ───────────────────────────────────────────────────────
# БЕЗ ЭТИХ ТРЁХ СТРОК СООБЩЕНИЯ УРОВНЯ INFO ИЗ НАШЕГО КОДА НЕ ВИДНЫ НИГДЕ,
# и это не теория: 10.08.2026 миграция NDS-CLEANUP ③ отработала на проде,
# а строка «колонки удалены» в Deploy Logs не появилась. Полчаса ушло на то,
# чтобы отличить «сделано молча» от «не выполнялось» — различить удалось
# только запросом к information_schema, лог не помог никак.
#
# ПОЧЕМУ ТАК БЫЛО (замерено настоящим uvicorn, а не прочитано в исходниках):
# uvicorn применяет свой LOGGING_CONFIG и настраивает В НЁМ ТОЛЬКО три
# логгера — uvicorn, uvicorn.access, uvicorn.error. Корневой логгер остаётся
# без обработчиков и с уровнем WARNING, поэтому наши `app.*` логгеры
# доходят до logging.lastResort, а он тоже WARNING: warning виден, info
# отбрасывается ДО форматирования.
#
# ПОЧЕМУ ЗДЕСЬ И ПОЧЕМУ ЭТО ПЕРЕЖИВЁТ ЗАПУСК: тем же замером проверено, что
# LOGGING_CONFIG применяется ДО импорта приложения (на момент импорта у
# uvicorn.error уже стоит уровень INFO из конфига). Значит настройка,
# сделанная при импорте `app.main`, не перетирается — обработчик на месте
# и на старте, и дальше. На импорте наш код ничего не пишет, поэтому строк
# не теряется. `basicConfig` без force намеренно: если обработчики корня
# кто-то уже поставил (pytest), вызов не делает ничего и чужой захват
# сообщений не ломается.
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
    stream=sys.stdout,
)

# Sentry (error-capture). init ДО создания app, чтобы интеграция
# инструментировала всё приложение. Пустой SENTRY_DSN → init пропускается
# (паттерн RESEND_API_KEY): локалка и CI стартуют без Sentry.
init_sentry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="AOCG AI Офис API", lifespan=lifespan)

# Rate limiting (slowapi) — used by the login endpoint.
app.state.limiter = auth.limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# Catch-all для необработанных исключений (500): структурный лог + generic-JSON
# без traceback. HTTPException/RateLimitExceeded не перехватывает (свои хендлеры).
app.add_exception_handler(Exception, unhandled_exception_handler)

# Security middleware (rate-limit per IP, авто-бан, security-headers, enforce
# HTTPS). Конфиг через env SECURITY_* — все НЕ обязательны (дефолты: 60/мин,
# /api/auth/* 5/мин, бан после 10 превышений, enforce_https=true). HTTPS-проверка
# смотрит x-forwarded-proto (Railway за прокси), поэтому прод проходит; локально
# выключается SECURITY_ENFORCE_HTTPS=false.
# ВАЖНО (порядок): добавляем ДО CORS, чтобы CORS остался добавленным ПОСЛЕДНИМ
# = ВНЕШНИМ слоем — тогда даже 429/403 от security проходят через CORS и
# получают CORS-заголовки, а preflight OPTIONS обрабатывается CORS первым.
app.add_middleware(AOCGSecurityMiddleware)

# CORS-whitelist через env CORS_ORIGINS — запятая-разделённый список origin'ов
# (напр. "https://app.example.ru,https://example.ru"). Если переменная не задана —
# fallback "*" (как было: не ломает прод до выставления env и локальную разработку).
# На проде выставить CORS_ORIGINS в Railway = домен(ы) фронта → whitelist включится
# на ближайшем рестарте контейнера, без передеплоя кода. См. задачу S-19.
_cors_env = os.environ.get("CORS_ORIGINS", "").strip()
_cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()] or ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(receipts.router)
app.include_router(reports.router)
app.include_router(fns.router)
app.include_router(cards.router)
app.include_router(ocr.router)
app.include_router(consent.router)
app.include_router(users.router)
app.include_router(services.router)
app.include_router(categories.router)
app.include_router(organizations.router)


@app.get("/")
async def root():
    return {"app": "AOCG AI Офис", "version": "1.0"}


@app.get("/health")
async def health():
    # Liveness для UptimeRobot: всегда 200, БД НЕ трогаем. Под общим
    # rate-лимитом 60/мин (не под auth 5/мин — путь не /api/auth/*).
    return {"status": "ok"}
