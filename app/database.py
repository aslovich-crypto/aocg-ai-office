import asyncpg
import json
import os

pool = None

async def _init_conn(conn):
    await conn.set_type_codec('jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog')

async def get_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(os.environ.get("DATABASE_URL"), init=_init_conn)
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
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS raw_data JSONB;
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'manual';
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS photo_url TEXT;
            CREATE TABLE IF NOT EXISTS cards (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
            INSERT INTO cards (name)
            SELECT * FROM (VALUES ('Личная карта'), ('Корпоративная карта')) AS v(name)
            WHERE NOT EXISTS (SELECT 1 FROM cards);
            CREATE TABLE IF NOT EXISTS user_consents (
                id              SERIAL PRIMARY KEY,
                user_id         TEXT NOT NULL,
                consent_at      TIMESTAMPTZ DEFAULT NOW(),
                ip_address      TEXT,
                policy_version  TEXT NOT NULL DEFAULT '1.0',
                consent_text    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS user_consents_user_id_consent_at_idx
                ON user_consents(user_id, consent_at DESC);
        """)
