from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import init_db
from app.routers import receipts, reports, fns, cards, ocr, consent, users, services


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="AOCG AI Офис API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(receipts.router)
app.include_router(reports.router)
app.include_router(fns.router)
app.include_router(cards.router)
app.include_router(ocr.router)
app.include_router(consent.router)
app.include_router(users.router)
app.include_router(services.router)

@app.get("/")
async def root():
    return {"app": "AOCG AI Офис", "version": "1.0"}
