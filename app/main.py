from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.database import init_db
from app.routers import receipts, reports, fns, cards, ocr, consent, users, services, auth, categories


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="AOCG AI Офис API", lifespan=lifespan)

# Rate limiting (slowapi) — used by the login endpoint.
app.state.limiter = auth.limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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
async def root(request: Request):
    # ── ВРЕМЕННАЯ XFF-проба (revert after). Логирует ТОЛЬКО при ?probe=xff,
    # чтобы не шуметь и не писать IP обычных пользователей. Ответ не меняется.
    if request.query_params.get("probe") == "xff":
        SENSITIVE = ("authorization", "cookie", "proxy-authorization")
        h = request.headers
        print(
            "[HDRPROBE]"
            f" client_host={request.client.host if request.client else None}"
            f" | x-forwarded-for={h.get('x-forwarded-for')!r}"
            f" | x-real-ip={h.get('x-real-ip')!r}"
            f" | x-envoy-external-address={h.get('x-envoy-external-address')!r}"
            f" | x-forwarded-proto={h.get('x-forwarded-proto')!r}"
            f" | header_keys={sorted(h.keys())}"
            f" | sensitive={ {k: ('present' if h.get(k) else 'absent') for k in SENSITIVE} }",
            flush=True,
        )
    return {"app": "AOCG AI Офис", "version": "1.0"}
