import asyncpg
import os

pool = None

async def get_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(os.environ.get("DATABASE_URL"))
    return pool

async def init_db():
    p = await get_pool()
    async with p.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS receipts (
                id SERIAL PRIMARY KEY,
                date DATE NOT NULL,
                org VARCHAR(255) NOT NULL,
                category VARCHAR(100),
                payment VARCHAR(100),
                amount NUMERIC(12,2),
                employee VARCHAR(255),
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                status VARCHAR(50) DEFAULT 'Личные',
                total NUMERIC(12,2),
                created DATE DEFAULT CURRENT_DATE,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS report_items (
                report_id INTEGER REFERENCES reports(id) ON DELETE CASCADE,
                receipt_id INTEGER REFERENCES receipts(id)
            );
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS fn TEXT;
            CREATE UNIQUE INDEX IF NOT EXISTS receipts_fn_unique
                ON receipts(fn) WHERE fn IS NOT NULL;
        """)
