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
            ALTER TABLE cards ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE;
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
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                first_name    TEXT,
                last_name     TEXT,
                patronymic    TEXT,
                email         TEXT,
                inn           TEXT,
                region        TEXT DEFAULT 'Россия',
                employee_id   TEXT,
                role          TEXT DEFAULT 'employee',
                is_active     BOOLEAN DEFAULT true,
                created_at    TIMESTAMPTZ DEFAULT NOW()
            );
            INSERT INTO users (first_name, last_name, patronymic, email, role)
            SELECT 'Алексей', 'Шукалович', 'Иванович', 'a.slovich@gmail.com', 'admin'
            WHERE NOT EXISTS (SELECT 1 FROM users);
            -- Backfill: receipts with a fiscal number came via QR/FNS, not manual.
            UPDATE receipts SET source='qr_scan'
            WHERE (fn IS NOT NULL AND fn <> '') AND (source IS NULL OR source='manual');

            -- ─── AUTH & ORGANIZATIONS (feat/auth-system) ───
            CREATE TABLE IF NOT EXISTS organizations (
                id          SERIAL PRIMARY KEY,
                name        TEXT NOT NULL,
                inn         TEXT,
                type        TEXT NOT NULL DEFAULT 'company',  -- 'person' | 'company'
                owner_id    INTEGER,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
            ALTER TABLE users ADD COLUMN IF NOT EXISTS org_id INTEGER REFERENCES organizations(id);
            ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS is_email_verified BOOLEAN DEFAULT false;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_token TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_attempts INTEGER DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS org_id INTEGER;
            ALTER TABLE reports  ADD COLUMN IF NOT EXISTS org_id INTEGER;
            ALTER TABLE cards    ADD COLUMN IF NOT EXISTS org_id INTEGER;
            CREATE TABLE IF NOT EXISTS invite_links (
                id          SERIAL PRIMARY KEY,
                token       TEXT UNIQUE NOT NULL,
                org_id      INTEGER NOT NULL REFERENCES organizations(id),
                role        TEXT NOT NULL DEFAULT 'employee',
                created_by  INTEGER REFERENCES users(id),
                expires_at  TIMESTAMPTZ NOT NULL,
                max_uses    INTEGER DEFAULT 1,
                uses_count  INTEGER DEFAULT 0,
                is_active   BOOLEAN DEFAULT true,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS revoked_tokens (
                id          SERIAL PRIMARY KEY,
                token_hash  TEXT NOT NULL,
                expires_at  TIMESTAMPTZ NOT NULL
            );
            -- Bootstrap a default org so existing single-tenant data isn't orphaned
            -- once org filtering turns on; assign all current rows to it.
            INSERT INTO organizations (name, type, owner_id)
            SELECT 'АОЦГ', 'company', (SELECT id FROM users ORDER BY id LIMIT 1)
            WHERE NOT EXISTS (SELECT 1 FROM organizations);
            UPDATE users    SET org_id=(SELECT id FROM organizations ORDER BY id LIMIT 1) WHERE org_id IS NULL;
            UPDATE receipts SET org_id=(SELECT id FROM organizations ORDER BY id LIMIT 1) WHERE org_id IS NULL;
            UPDATE reports  SET org_id=(SELECT id FROM organizations ORDER BY id LIMIT 1) WHERE org_id IS NULL;
            UPDATE cards    SET org_id=(SELECT id FROM organizations ORDER BY id LIMIT 1) WHERE org_id IS NULL;
        """)

