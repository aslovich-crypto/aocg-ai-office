import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.database import get_pool, init_db
from app import env_check
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
    max_relay,
    search,
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
# не теряется.
#
# `force=True` ЗДЕСЬ НЕ ЗАБЫТ — ЕГО НЕТ НАМЕРЕННО, И ЭТО ВАЖНО. force удаляет
# ВСЕ обработчики корня перед настройкой. В контейнере их нет, разницы бы не
# было; а вот в тестах обработчик корня ставит pytest (caplog, вывод по
# упавшему тесту), и наш импорт снёс бы его — захват сообщений сломался бы
# молча, потому что тесты и без логов зелёные. Без force вызов НИЧЕГО не
# делает, когда обработчики уже есть: контейнер получает настройку, pytest
# остаётся при своей. Если однажды понадобится перенастроить журнал поверх
# чужого — это отдельное решение, а не «дописать force».
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
    # ⚠️ ПЕРВЫМ ДЕЛОМ, ДО БАЗЫ: настройка проверяется раньше, чем хоть что-то
    # начинает работать. Ошибка настройки обязана выглядеть как отказ старта —
    # заметный сразу, — а не как странное письмо через неделю (T62).
    auth.проверить_адрес_фронта()
    # ⚠️ СВЕРКА ОКРУЖЕНИЯ С ОПИСЬЮ — ЕДИНСТВЕННЫЙ СПОСОБ УВИДЕТЬ ПАНЕЛЬ
    # СНАРУЖИ (S-49, просьба владельца 04.09.2026). Доступа внутрь
    # контейнера нет, консоли может не быть, а журнал деплоя виден в панели
    # всегда. Старт НЕ роняем: расхождение с документом может быть
    # осознанным, и уронить прод из-за несогласия с текстом — цена выше
    # пользы. Говорим — решает человек.
    env_check.сверить_и_сказать()
    await init_db()
    yield


# ⚠️ СХЕМА API ЗАКРЫТА НА ПРОДЕ (S-64). `/docs`, `/redoc` и `/openapi.json`
# отдавали 200 кому угодно (замер 04.09.2026, все три). Данных это не отдаёт
# и «утечкой» звать неверно — отдаёт СТРУКТУРУ: имена всех ручек, формы
# запросов и ответов, включая `/internal/max/*`, вся защита которых держится
# на одном статическом токене. Сканеру это оглавление, выданное бесплатно.
#
# ⚠️ ЗАКРЫТО ПЕРЕКЛЮЧАТЕЛЕМ, А НЕ НАСМЕРТЬ: схема — рабочий инструмент,
# и отнимать его у разработки нельзя. По умолчанию ОТКРЫТО там, где
# ENVIRONMENT не «production»; на проде закрыто. Открыть на проде можно
# явным API_DOCS_PUBLIC=true — временно и осознанно.
#
# Умолчание выбрано «закрыто на проде», а не «открыто, пока не запретили»:
# это ровно правило S-49 — незаданная переменная не должна ослаблять защиту.
_прод = os.environ.get("ENVIRONMENT", "production").strip().lower() == "production"
_схема_открыта = (
    os.environ.get("API_DOCS_PUBLIC", "").strip().lower() in ("1", "true", "yes", "on")
    or not _прод
)

app = FastAPI(
    title="AOCG AI Офис API",
    lifespan=lifespan,
    docs_url="/docs" if _схема_открыта else None,
    redoc_url="/redoc" if _схема_открыта else None,
    openapi_url="/openapi.json" if _схема_открыта else None,
)

# Rate limiting (slowapi) — used by the login endpoint.
app.state.limiter = auth.limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# Catch-all для необработанных исключений (500): структурный лог + generic-JSON
# без traceback. HTTPException/RateLimitExceeded не перехватывает (свои хендлеры).
app.add_exception_handler(Exception, unhandled_exception_handler)

# Security middleware (rate-limit per IP, авто-бан, security-headers, enforce
# HTTPS). Конфиг через env SECURITY_* — все НЕ обязательны (дефолты: 60/мин,
# /api/auth/* 5/мин, бан после 10 превышений, enforce_https=true). HTTPS-проверка
# смотрит x-forwarded-proto (площадка держит прод за прокси), поэтому прод проходит; локально
# выключается SECURITY_ENFORCE_HTTPS=false.
# ВАЖНО (порядок): добавляем ДО CORS, чтобы CORS остался добавленным ПОСЛЕДНИМ
# = ВНЕШНИМ слоем — тогда даже 429/403 от security проходят через CORS и
# получают CORS-заголовки, а preflight OPTIONS обрабатывается CORS первым.
app.add_middleware(AOCGSecurityMiddleware)

# CORS-whitelist через env CORS_ORIGINS — запятая-разделённый список origin'ов
# (напр. "https://app.example.ru,https://example.ru"). Если переменная не задана —
# fallback "*" (как было: не ломает прод до выставления env и локальную разработку).
# На проде выставить CORS_ORIGINS в панели Timeweb = домен(ы) фронта → whitelist включится
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
app.include_router(max_relay.router)
app.include_router(search.router)


@app.get("/")
async def root():
    return {"app": "AOCG AI Офис", "version": "1.0"}


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    """Liveness для мониторинга: всегда 200, БД НЕ трогаем.

    HEAD ЗДЕСЬ ОБЯЗАТЕЛЕН, И ЭТО КУПЛЕНО ПОЛУТОРА МЕСЯЦАМИ ЛОЖНОЙ ТРЕВОГИ.
    Мониторы ходят методом HEAD — тела им не нужно, нужен код ответа.
    FastAPI, в отличие от голого Starlette, НЕ добавляет HEAD к маршруту
    с `@app.get`: на HEAD приходил `405 Method Not Allowed` с заголовком
    `allow: GET`. Снаружи это выглядело изощрённо: прибор исправно измерял
    время ответа (125 мс — значит сервер отвечал), но статус считал
    падением, и монитор показывал DOWN 1 мес 16 дней подряд при доступности
    0.000% и зелёном графике скорости.

    Хуже молчащего прибора: на постоянно красный перестают смотреть,
    и настоящее падение пройдёт незамеченным. Разбор — T46.

    Под общим rate-лимитом 60/мин (не под auth 5/мин — путь не /api/auth/*).
    """
    return {"status": "ok"}


@app.api_route("/health/db", methods=["GET", "HEAD"])
async def health_db():
    """Readiness: жива ли БАЗА, а не только веб-процесс (решение владельца 04.09.2026).

    Зачем вторая ручка, когда есть /health: та намеренно не трогает базу —
    и потому смерть базы или пула для монитора невидима: health зелёный,
    а люди видят ошибку на каждом чеке. Класс «зелёный прибор при мёртвом
    продукте» — ровно тот, что кусал весь август (T43/T46).

    ⚠️ ТОЛЬКО SELECT 1, БЕЗ ВНЕШНИХ ЗАВИСИМОСТЕЙ — и это граница по решению
    владельца: ФНС, распознавание и почта сюда не входят, чужой сбой не должен
    красить НАШ аптайм. Ответ без деталей: текст ошибки может содержать DSN,
    наружу уходит только слово, причина — в журнал.

    HEAD обязателен (урок T46: мониторы ходят HEAD-ом, @app.get его не даёт).
    Таймаут свой, 5 с: зависшая база без него превращала бы ответ в вечное
    ожидание, и монитор резал бы по своему таймауту без нашего диагноза.
    """
    try:
        p = await get_pool()
        await asyncio.wait_for(p.fetchval("SELECT 1"), timeout=5)
    except Exception:
        logging.getLogger("app.health").warning(
            "health/db: база не отвечает", exc_info=True
        )
        return JSONResponse({"status": "db_unavailable"}, status_code=503)
    return {"status": "ok"}
