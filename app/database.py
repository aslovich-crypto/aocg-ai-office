import asyncpg
import json
import os

from app.categories_seed import seed_default_categories

pool = None


async def _init_conn(conn):
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def get_pool():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(
            os.environ.get("DATABASE_URL"), init=_init_conn
        )
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

            -- ─── AUTH & ORGANIZATIONS (feat/auth-system) ───
            CREATE TABLE IF NOT EXISTS organizations (
                id          SERIAL PRIMARY KEY,
                name        TEXT NOT NULL,
                inn         TEXT,
                type        TEXT NOT NULL DEFAULT 'company',  -- 'person' | 'company'
                owner_id    INTEGER,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
            -- Налоговый режим организации (задача INT, блок «Налоговый учёт»):
            -- osno | usn_d | usn_dr | psn | npd | eshn. NULL = не указан.
            -- Откат: ALTER TABLE organizations DROP COLUMN tax_system;
            ALTER TABLE organizations ADD COLUMN IF NOT EXISTS tax_system VARCHAR(30);
            ALTER TABLE users ADD COLUMN IF NOT EXISTS org_id INTEGER REFERENCES organizations(id);
            ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS is_email_verified BOOLEAN DEFAULT false;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_token TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_attempts INTEGER DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS employee_number VARCHAR(20);
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
            -- Permanent (no-expiry) invite links: expires_at may be NULL.
            ALTER TABLE invite_links ALTER COLUMN expires_at DROP NOT NULL;
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
            -- Founding user (id=1) is the organization administrator.
            UPDATE users SET role='admin' WHERE id=1;

            -- ============================================================
            -- Расширение схемы (Чекпойнт A задачи №7 / AOCG-DIR-AI-002 v10)
            -- Добавляет 20 колонок + receipt_items + 5 индексов.
            -- Старые колонки (org, payment, date, amount, employee) НЕ удаляются.
            -- Колонка fn выведена из обращения (канон — kkt_fn); сам DROP COLUMN fn —
            -- отдельным ЧП, здесь init_db её больше не трогает.
            -- ============================================================
            -- Обязательные (10 — fn уже есть; amount оставляем старую NUMERIC(12,2)):
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS datetime       TIMESTAMP WITH TIME ZONE;
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS currency       VARCHAR(3)  DEFAULT 'RUB';
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS operation_type VARCHAR(20) DEFAULT 'purchase';
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS org_legal      VARCHAR(500);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS org_brand      VARCHAR(200);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS org_inn        VARCHAR(12);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS payment_form   VARCHAR(20);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS payment_detail VARCHAR(100);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS card_last4     VARCHAR(4);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS card_id        INTEGER REFERENCES cards(id);
            -- Желательные (5):
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS tax_system     VARCHAR(30);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS address        TEXT;
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS vat_20         NUMERIC(15,2);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS vat_10         NUMERIC(15,2);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS vat_0          NUMERIC(15,2);
            -- Фискальные (5 — fn уже есть, переименуем в Чекпойнте C):
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS kkt_serial     VARCHAR(20);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS kkt_rn         VARCHAR(20);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS fd_num         VARCHAR(20);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS fpd            VARCHAR(20);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS cashier        VARCHAR(200);

            -- Позиции чека (1 чек → N позиций). Каскадно удаляются с чеком.
            CREATE TABLE IF NOT EXISTS receipt_items (
                id         SERIAL PRIMARY KEY,
                receipt_id INTEGER REFERENCES receipts(id) ON DELETE CASCADE,
                position   INTEGER NOT NULL,
                name       VARCHAR(500) NOT NULL,
                quantity   NUMERIC(10,3),
                price      NUMERIC(15,2),
                sum        NUMERIC(15,2),
                vat_rate   VARCHAR(10),
                created_at TIMESTAMP DEFAULT NOW()
            );

            -- 5 индексов (idx_receipts_org_id был пропущен в прежней схеме):
            CREATE INDEX IF NOT EXISTS idx_receipts_datetime        ON receipts(datetime);
            CREATE INDEX IF NOT EXISTS idx_receipts_org_inn         ON receipts(org_inn);
            CREATE INDEX IF NOT EXISTS idx_receipts_card_id         ON receipts(card_id);
            CREATE INDEX IF NOT EXISTS idx_receipts_org_id          ON receipts(org_id);
            CREATE INDEX IF NOT EXISTS idx_receipt_items_receipt_id ON receipt_items(receipt_id);

            -- ── Чекпойнт C задачи №7: kkt_fn — канонический фискальный номер ──
            -- Колонка fn и backfill kkt_fn=fn убраны (kkt_fn устаканился, пишется в
            -- INSERT напрямую; DROP COLUMN fn — отдельным ЧП).
            -- Уникальность документа — по ПАРЕ (kkt_fn=ФН, fd_num=ФД): ФН один на
            -- кассу, общий для всех чеков; уникален документ только парой. Старый
            -- одиночный receipts_kkt_fn_unique дропается на проде явной миграцией
            -- (CREATE IF NOT EXISTS здесь его НЕ удаляет).
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS kkt_fn VARCHAR(20);
            CREATE UNIQUE INDEX IF NOT EXISTS receipts_kkt_fn_fd_unique
                ON receipts(kkt_fn, fd_num) WHERE kkt_fn IS NOT NULL AND fd_num IS NOT NULL;

            -- ── Фикс №1 фаза A: справочник категорий расходов (11 групп / 48 статей) ──
            -- per-org копии (каждая орг владеет своими); receipts.category_id ссылается
            -- на categories.id (канон; старая строковая колонка category удалена).
            CREATE TABLE IF NOT EXISTS category_groups (
                id          SERIAL PRIMARY KEY,
                org_id      INTEGER NOT NULL REFERENCES organizations(id),
                name        TEXT NOT NULL,
                position    INTEGER NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (org_id, name)
            );
            CREATE TABLE IF NOT EXISTS categories (
                id          SERIAL PRIMARY KEY,
                org_id      INTEGER NOT NULL REFERENCES organizations(id),
                group_id    INTEGER NOT NULL REFERENCES category_groups(id),
                name        TEXT NOT NULL,
                tax_kind    TEXT NOT NULL,
                position    INTEGER NOT NULL,
                is_default  BOOLEAN DEFAULT TRUE,
                is_visible  BOOLEAN DEFAULT TRUE,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (org_id, name),
                CHECK (tax_kind IN (
                    'Материальные расходы','Прочие расходы','Командировочные расходы',
                    'Представительские расходы','Расходы на рекламу (нормируемые)',
                    'Транспортные расходы','Оплата труда','Налоги и сборы',
                    'Не учитываемые в целях налогообложения'
                ))
            );
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS category_id INTEGER REFERENCES categories(id);
            -- Смена категории чека: TRUE после ручного выбора пользователем — будущий
            -- батч-пересчёт (Фикс №4) такие чеки не трогает (WHERE category_manual=FALSE).
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS category_manual BOOLEAN DEFAULT FALSE;
            CREATE INDEX IF NOT EXISTS idx_receipts_category_id   ON receipts(category_id);
            CREATE INDEX IF NOT EXISTS idx_categories_org_id      ON categories(org_id);
            CREATE INDEX IF NOT EXISTS idx_category_groups_org_id ON category_groups(org_id);
        """)

        # ── Фикс №1 фаза A: seed дефолтных категорий + бэкфилл category_id ──
        # DDL выше идемпотентен; seed/бэкфилл — на Python (нужны id созданных групп).
        # Каждой орг без категорий засеваем 11+48; затем старые строковые category
        # мапим в category_id (per-org, по имени дефолтной статьи). Всё в одной
        # транзакции; seed_default_categories сам no-op для уже засеянных орг.
        async with conn.transaction():
            org_ids = [
                r["id"] for r in await conn.fetch("SELECT id FROM organizations")
            ]
            for org_id in org_ids:
                await seed_default_categories(conn, org_id)
