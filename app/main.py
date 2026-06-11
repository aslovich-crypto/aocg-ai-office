from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.database import init_db
from app.routers import receipts, reports, fns, cards, ocr, consent, users, services, auth, categories
from aocg_security.middleware import AOCGSecurityMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="AOCG AI Офис API", lifespan=lifespan)

# Rate limiting (slowapi) — used by the login endpoint.
app.state.limiter = auth.limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Security middleware (rate-limit per IP, авто-бан, security-headers, enforce
# HTTPS). Конфиг через env SECURITY_* — все НЕ обязательны (дефолты: 60/мин,
# /api/auth/* 5/мин, бан после 10 превышений, enforce_https=true). HTTPS-проверка
# смотрит x-forwarded-proto (Railway за прокси), поэтому прод проходит; локально
# выключается SECURITY_ENFORCE_HTTPS=false.
# ВАЖНО (порядок): добавляем ДО CORS, чтобы CORS остался добавленным ПОСЛЕДНИМ
# = ВНЕШНИМ слоем — тогда даже 429/403 от security проходят через CORS и
# получают CORS-заголовки, а preflight OPTIONS обрабатывается CORS первым.
app.add_middleware(AOCGSecurityMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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

@app.get("/")
async def root():
    return {"app": "AOCG AI Офис", "version": "1.0"}
