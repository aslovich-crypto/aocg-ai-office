from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import init_db
from app.routers import receipts, reports, fns

app = FastAPI(title="AOCG AI Офис API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    await init_db()

app.include_router(receipts.router)
app.include_router(reports.router)
app.include_router(fns.router)

@app.get("/")
async def root():
    return {"app": "AOCG AI Офис", "version": "1.0"}
