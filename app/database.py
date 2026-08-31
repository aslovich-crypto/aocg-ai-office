import asyncpg
import json
import logging
import os

from app.categories_seed import seed_default_categories

logger = logging.getLogger(__name__)

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


async def _снять_колонки_ставок_ндс(conn) -> None:
    """NDS-CLEANUP ③: убрать `vat_20`/`vat_10` — но ТОЛЬКО если ничего не теряем.

    Почему дроп с проверкой, а не строкой DDL в общем скрипте: удаление
    колонки необратимо, а «ничего не потеряем» — это утверждение о ДАННЫХ,
    и проверять его надо в момент действия, а не по вчерашнему замеру.
    Замер, сделанный за день до выкатки, к моменту старта контейнера
    может устареть; здесь он выполняется прямо перед `DROP`.

    Что считается потерей: чек, у которого НДС есть ТОЛЬКО в этих колонках —
    ни в разбивке, ни в `vat_total`. На 10.08.2026 таких ноль, и появиться
    новым неоткуда: запись в колонки прекращена шагом ②. Но если такой чек
    всё же найдётся, колонки ОСТАЮТСЯ, а в журнал уходит предупреждение:
    молча удалять чужой НДС нельзя, а падать на старте — значит уронить
    приложение целиком из-за уборки.

    Идемпотентно: колонок нет — выходим сразу.
    Откат (если колонки понадобятся снова, ДАННЫЕ НЕ ВЕРНУТСЯ):
        ALTER TABLE receipts ADD COLUMN vat_20 NUMERIC(15,2);
        ALTER TABLE receipts ADD COLUMN vat_10 NUMERIC(15,2);
    """
    есть = await conn.fetchval(
        """SELECT count(*) FROM information_schema.columns
           WHERE table_name='receipts' AND column_name IN ('vat_20','vat_10')"""
    )
    if not есть:
        return

    рискуют = await conn.fetchval(
        """SELECT count(*) FROM receipts
           WHERE (COALESCE(vat_20,0) <> 0 OR COALESCE(vat_10,0) <> 0)
             AND (vat_breakdown IS NULL OR vat_breakdown = '{}'::jsonb)
             AND COALESCE(vat_total,0) = 0"""
    )
    if рискуют:
        logger.warning(
            "NDS-CLEANUP ③ ОТМЕНЁН: у %s чеков НДС есть ТОЛЬКО в vat_20/vat_10 "
            "(ни разбивки, ни vat_total). Колонки НЕ удалены — сначала перенести "
            "эти суммы, иначе НДС исчезнет без следа.",
            рискуют,
        )
        return

    await conn.execute("ALTER TABLE receipts DROP COLUMN IF EXISTS vat_20")
    await conn.execute("ALTER TABLE receipts DROP COLUMN IF EXISTS vat_10")
    # УРОВЕНЬ ВЫБРАН НАМЕРЕННО, И ЭТО НЕ ОПЕЧАТКА. Успех разовой миграции
    # печатается так же заметно, как отказ: раньше здесь стоял `info`, при
    # корневом уровне WARNING он был отброшен, и «сделано» в логах выглядело
    # ровно как «не выполнялось». Настройку журнала мы починили (app/main.py),
    # но исход НЕОБРАТИМОЙ операции не должен зависеть от того, настроен ли
    # журнал вообще: `warning` доходит даже через logging.lastResort.
    logger.warning("NDS-CLEANUP ③: колонки vat_20/vat_10 удалены, потерь нет")


async def _засеять_первого_администратора(conn) -> None:
    """Первый администратор на ПУСТОЙ базе — из окружения, не из исходника (S-23).

    Раньше ФИО и почта живого человека стояли строкой в DDL внутри `init_db()`,
    то есть персональные данные лежали в репозитории и уезжали в каждую его
    копию — включая форки, архивы и чужие машины. Здесь их нет: имена берутся
    из переменных окружения и подставляются ПАРАМЕТРАМИ.

    Переменных нет — засев ПРОПУСКАЕТСЯ, и это осознанный выбор. Придумывать
    администратора «по умолчанию» хуже пустой базы: учётная запись с чужим или
    выдуманным адресом выглядит настоящей и переживает не одну миграцию.
    На рабочей базе строка давно есть, поэтому пропуск ничего не меняет;
    на новой — администратор заводится штатной регистрацией.
    """
    почта = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "").strip()
    if not почта:
        return
    await conn.execute(
        """
        INSERT INTO users (first_name, last_name, patronymic, email, role)
        SELECT $1, $2, $3, $4, 'admin'
        WHERE NOT EXISTS (SELECT 1 FROM users)
        """,
        os.getenv("BOOTSTRAP_ADMIN_FIRST_NAME", "").strip() or None,
        os.getenv("BOOTSTRAP_ADMIN_LAST_NAME", "").strip() or None,
        os.getenv("BOOTSTRAP_ADMIN_PATRONYMIC", "").strip() or None,
        почта,
    )


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
                status VARCHAR(50) DEFAULT 'Черновик',
                total NUMERIC(12,2),
                created DATE DEFAULT CURRENT_DATE,
                created_at TIMESTAMP DEFAULT NOW()
            );
            -- report_items — фактически 1:N, НЕ M:N: отчёт → много чеков,
            -- чек → РОВНО ОДИН отчёт. Это правило про деньги: один чек в двух
            -- авансовых отчётах = двойное возмещение сотруднику и задвоение
            -- расхода в налоговом учёте (ст. 252 НК РФ требует документального
            -- подтверждения; один документ не может обосновывать две записи).
            -- Фиксируется индексом uq_report_items_receipt_id ниже. Не читать
            -- таблицу как «многие-ко-многим» — прежние описания её так называли
            -- ошибочно, из-за этого правило держалось только на фронте.
            CREATE TABLE IF NOT EXISTS report_items (
                report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                receipt_id INTEGER NOT NULL REFERENCES receipts(id)
            );
            -- Для СУЩЕСТВУЮЩИХ баз CREATE TABLE выше — no-op (IF NOT EXISTS
            -- пропускает таблицу целиком), поэтому те же правила навешиваем
            -- идемпотентными ALTER/INDEX. UNIQUE делаем индексом, а не
            -- ADD CONSTRAINT: init_db крутится на КАЖДОМ старте контейнера,
            -- а ADD CONSTRAINT не идемпотентен и уронил бы приложение.
            -- Откат: DROP INDEX idx_report_items_report_id;
            --        DROP INDEX uq_report_items_receipt_id;
            --        ALTER TABLE report_items ALTER COLUMN receipt_id DROP NOT NULL;
            --        ALTER TABLE report_items ALTER COLUMN report_id  DROP NOT NULL;
            ALTER TABLE report_items ALTER COLUMN report_id  SET NOT NULL;
            ALTER TABLE report_items ALTER COLUMN receipt_id SET NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS uq_report_items_receipt_id
                ON report_items(receipt_id);
            CREATE INDEX IF NOT EXISTS idx_report_items_report_id
                ON report_items(report_id);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS raw_data JSONB;
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'manual';
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS photo_url TEXT;
            -- photo_key — КЛЮЧ ОБЪЕКТА в приватном бакете (задача №3), а не адрес.
            -- Почему отдельная колонка, а не photo_url: у приватного бакета
            -- постоянного адреса нет, ссылка подписывается на 5 минут в момент
            -- запроса. Смешать в одной колонке ключ и готовый URL — это «одно
            -- название, разное содержимое», и оно уже закреплено пятью тестами
            -- на photo_url. Обратный DDL (точка отката):
            --     ALTER TABLE receipts DROP COLUMN photo_key;
            -- Проверено на проде 12.08.2026 внутри BEGIN/ROLLBACK: колонка
            -- создаётся, повторный прогон идемпотентен, запись читается,
            -- после отката колонки нет.
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS photo_key TEXT;
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
            -- Первый администратор здесь БОЛЬШЕ НЕ ЗАСЕВАЕТСЯ (S-23): его ФИО
            -- и почта были вписаны в исходник прямо в этот INSERT. Засев уехал
            -- в _засеять_первого_администратора() ниже — читает окружение
            -- и работает параметризованным запросом.

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
            -- Срок жизни ссылки подтверждения (T75, 72 часа). Отдельная
            -- колонка, а НЕ вычисление от created_at: после появления
            -- переотправки (S-83) токен переиздаётся, и время создания
            -- ПОЛЬЗОВАТЕЛЯ перестаёт быть временем выдачи ТОКЕНА —
            -- новый токен родился бы уже мёртвым.
            -- NULL означает «выдан до введения срока»: такие проверяются
            -- по created_at, отдельного кода миграции для них не пишем.
            -- Основание — замер с бастиона 27.08.2026: строк с непустым
            -- токеном 5, все тестовые, живых пользователей за ними нет.
            -- Обратный DDL: ALTER TABLE users DROP COLUMN email_verify_expires_at;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_expires_at TIMESTAMPTZ;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_attempts INTEGER DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS employee_number VARCHAR(20);
            -- Отзыв токенов без чёрного списка (S-16): токен, выданный РАНЬШЕ
            -- этой отметки, недействителен. Ставится при смене пароля и по
            -- «выйти на всех устройствах». Проверка живёт в get_current_user
            -- и не стоит лишнего запроса — строка пользователя и так читается
            -- на каждом вызове API.
            -- NULL = «никого не выгоняли»; это состояние по умолчанию после
            -- выкатки, иначе разлогинило бы всех разом без нужды.
            -- Откат: ALTER TABLE users DROP COLUMN tokens_valid_from;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS tokens_valid_from TIMESTAMPTZ;
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS org_id INTEGER;
            ALTER TABLE reports  ADD COLUMN IF NOT EXISTS org_id INTEGER;
            ALTER TABLE cards    ADD COLUMN IF NOT EXISTS org_id INTEGER;
            -- Жизненный цикл статусов отчёта: Черновик → На проверке → Одобрен/Отклонён.
            -- CREATE TABLE выше с IF NOT EXISTS на существующей БД — no-op, поэтому дефолт
            -- существующей колонки меняем явным идемпотентным ALTER (был 'Личные').
            -- Откат: ALTER TABLE reports ALTER COLUMN status SET DEFAULT 'Личные';
            ALTER TABLE reports  ALTER COLUMN status SET DEFAULT 'Черновик';
            -- REP-AUTHOR: подотчётное лицо отчёта. Авансовый отчёт (АО-1) —
            -- документ КОНКРЕТНОГО сотрудника, «отчёта без автора» не бывает.
            -- Целевое состояние — NOT NULL, но добавляем в ДВА деплоя:
            --   ① колонка (nullable) + индекс + бэкфилл   ← этот деплой
            --   ③ ALTER COLUMN user_id SET NOT NULL       ← следующий деплой,
            --      когда весь код гарантированно пишет автора.
            -- Иначе строка, созданная старым кодом между деплоями, уронит
            -- контейнер на SET NOT NULL.
            -- Откат: DROP INDEX idx_reports_user_id;
            --        ALTER TABLE reports DROP COLUMN user_id;
            ALTER TABLE reports ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id);
            CREATE INDEX IF NOT EXISTS idx_reports_user_id ON reports(user_id);
            -- ⚠️ БЭКФИЛЛ И SET NOT NULL ПЕРЕЕХАЛИ НИЖЕ, К БЛОКУ receipts.
            -- Они читают receipts.user_id (`SELECT MIN(rc.user_id)`), а эта
            -- колонка создаётся ALTER-ом в блоке receipts — на сотню строк
            -- ниже. На НЕПУСТОЙ базе колонка уже есть от прошлых деплоев,
            -- и порядок ни на чём не сказывается; на ЧИСТОЙ её ещё нет,
            -- и init_db падает с `column rc.user_id does not exist`.
            -- Замер 29.08.2026: 147 красных прогонов CI подряд с 07.08,
            -- ровно эта строка.
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
            -- T104: приглашение знает, КОМУ оно выписано, и когда его
            -- отправляли. До этого ссылка была предъявительской: в списке
            -- нельзя было увидеть, кому она уходила, и «отправить повторно»
            -- было некому. Валидация BEGIN/ROLLBACK на проде 31.08.2026:
            -- колонок 10 → 14 → 10 после отката, строк 5 на всех этапах,
            -- ни одна старая строка значений не получила.
            -- ⚠️ ЭТИ ЧЕТЫРЕ СТРОКИ — ТРЕТЬЯ КОПИЯ ОДНОГО И ТОГО ЖЕ DDL
            -- (первая — список МИГРАЦИЯ в scripts/validate_invite_columns.py,
            -- вторая — SQL, прогнанный на бастионе). Разойтись молча они
            -- не могут: tests/test_migration_invite_columns.py требует
            -- дословного совпадения с тем списком.
            ALTER TABLE invite_links ADD COLUMN IF NOT EXISTS email      TEXT;
            ALTER TABLE invite_links ADD COLUMN IF NOT EXISTS first_name TEXT;
            ALTER TABLE invite_links ADD COLUMN IF NOT EXISTS last_name  TEXT;
            ALTER TABLE invite_links ADD COLUMN IF NOT EXISTS sent_at    TIMESTAMPTZ;
            -- T105/T118: одна почта — один человек. До 31.08.2026 UNIQUE
            -- на users.email не было ВООБЩЕ, и в базе жили две строки
            -- с одним адресом в разных организациях. Вход брал ПЕРВУЮ
            -- ПОПАВШУЮСЯ (`fetchrow` без ORDER BY): какая из двух учёток
            -- ответит, было не определено, и верный пароль мог не подойти.
            -- ⚠️ ИНДЕКС ЧАСТИЧНЫЙ. Пустая почта — обычное значение для базы,
            -- а `UserCreate.email: str = ""` позволяет её записать. Обычный
            -- UNIQUE разрешил бы РОВНО ОДНОГО безпочтового человека на всю
            -- базу и отверг бы второго при заведении.
            -- ⚠️ IF NOT EXISTS ОБЯЗАТЕЛЕН: init_db крутится на каждом старте,
            -- упавшее создание индекса уронило бы запуск целиком (T89).
            -- Валидация BEGIN/ROLLBACK на проде 31.08.2026: дублей 0,
            -- индекс создался, после отката исчез, людей не тронуло.
            CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower ON users (lower(email)) WHERE email IS NOT NULL AND email <> '';
            CREATE TABLE IF NOT EXISTS revoked_tokens (
                id          SERIAL PRIMARY KEY,
                token_hash  TEXT NOT NULL,
                expires_at  TIMESTAMPTZ NOT NULL
            );
            -- S-56, восстановление пароля. ОТДЕЛЬНАЯ ТАБЛИЦА, а не колонки
            -- в users: сбросов за жизнь пользователя несколько, и по ним надо
            -- видеть, кто и когда сбрасывал. В колонках users всегда только
            -- последнее.
            --
            -- ⚠️ ХРАНИТСЯ ХЕШ, А НЕ ТОКЕН (как в revoked_tokens): утечка базы
            -- не должна давать работающих ссылок на смену чужого пароля.
            CREATE TABLE IF NOT EXISTS password_resets (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash  TEXT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at  TIMESTAMPTZ NOT NULL,
                used_at     TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS ix_password_resets_hash
                ON password_resets (token_hash);
            -- ⚠️ ПОПЫТКИ СЧИТАЮТСЯ ПО АДРЕСУ НЕЗАВИСИМО ОТ ТОГО, СУЩЕСТВУЕТ ОН
            -- ИЛИ НЕТ, И ЭТО НЕ ПЕДАНТИЗМ. Считай мы только существующие,
            -- пороги стали бы разными: 3 запроса в час на зарегистрированный
            -- адрес и 20 на любой другой. Ответ при этом одинаков, а ПОВЕДЕНИЕ
            -- различается — перебирающий узнаёт наши адреса по тому, где лимит
            -- наступает раньше. Канал не в теле ответа, а в поведении системы.
            --
            -- Побочная выгода: работа на обоих путях становится одинаковой
            -- (вставка + подсчёт), и разница во времени ответа сужается
            -- до одной вставки токена.
            --
            -- ⚠️ ХРАНИМ ХЕШ АДРЕСА, А НЕ АДРЕС: иначе таблица копила бы почту
            -- людей, которые у нас не регистрировались. Строки старше окна
            -- чистятся, долгого хранения нет.
            CREATE TABLE IF NOT EXISTS reset_attempts (
                id          SERIAL PRIMARY KEY,
                email_hash  TEXT NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS ix_reset_attempts_hash
                ON reset_attempts (email_hash, created_at);
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
            -- Автор чека (A-ACL): кто создал. NULLABLE навсегда — старые строки и
            -- пограничные кейсы без автора остаются валидны. Доступ по роли — в роутере.
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS user_id        INTEGER REFERENCES users(id);
            -- ⚠️ БЭКФИЛЛ reports.user_id СТОИТ ЗДЕСЬ, А НЕ У СВОЕЙ КОЛОНКИ,
            -- И ЭТО НЕ НЕБРЕЖНОСТЬ, А ЕДИНСТВЕННОЕ ВЕРНОЕ МЕСТО.
            -- Он читает receipts.user_id — строку выше. Пока он стоял в блоке
            -- reports, между ним и этим ALTER лежало сто строк чужого кода,
            -- и порядок глазами не проверялся: на непустой базе колонка уже
            -- есть от прошлых деплоев, дефект невидим. На ЧИСТОЙ базе —
            -- падение `column rc.user_id does not exist`.
            --
            -- ⚠️ ЧЕТЫРЕ ОПЕРАТОРА НЕРАЗДЕЛИМЫ: два бэкфилла и SET NOT NULL.
            -- Ограничение обязано идти ПОСЛЕ обоих — иначе строка без автора
            -- уронит контейнер на старте.
            --
            -- ЦЕНА, ЗАМЕР 29.08.2026: 147 красных прогонов CI подряд с 07.08.
            -- Дефект лежал с 31.07 (REP-AUTHOR) и был невидим, пока 07.08
            -- в CI не появился шаг «SQL через настоящий PostgreSQL» — прибор
            -- заработал и сразу нашёл настоящую находку.
            --
            -- ⚠️ init_db — ЕДИНСТВЕННЫЙ путь поднять схему с нуля: вторая
            -- среда, восстановление из копии, переезд. Приёмка правки —
            -- не «CI позеленел», а «развёртывание с нуля работает».
            -- Бэкфилл ②, идемпотентный (WHERE user_id IS NULL): автор = автор
            -- чеков состава; для отчёта без чеков — владелец организации.
            UPDATE reports SET user_id = (
                SELECT MIN(rc.user_id)
                FROM report_items ri JOIN receipts rc ON rc.id = ri.receipt_id
                WHERE ri.report_id = reports.id
            ) WHERE user_id IS NULL;
            UPDATE reports SET user_id = (
                SELECT owner_id FROM organizations o WHERE o.id = reports.org_id
            ) WHERE user_id IS NULL;
            -- ③ Второй деплой (REP-AUTHOR ЧП2): код выше уже на проде и всегда
            -- пишет user_id, бэкфилл ② отработал (0 строк с NULL) — значит
            -- ограничение можно закрепить. Идемпотентно: повторный SET NOT NULL
            -- на уже NOT NULL колонке — no-op, init_db на каждом старте не падает.
            -- Откат: ALTER TABLE reports ALTER COLUMN user_id DROP NOT NULL;
            ALTER TABLE reports ALTER COLUMN user_id SET NOT NULL;
            -- Желательные (5):
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS tax_system     VARCHAR(30);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS address        TEXT;
            -- vat_20/vat_10 ЗДЕСЬ БОЛЬШЕ НЕ СОЗДАЮТСЯ (NDS-CLEANUP ③).
            -- Их снимает _снять_колонки_ставок_ндс() ниже — с проверкой, что
            -- ничего не теряется. Строки ADD COLUMN убраны намеренно: оставь
            -- их рядом с дропом, и колонки создавались бы заново на каждом
            -- старте контейнера, а дроп их тут же сносил.
            -- ⚠️ vat_0 — УСТАРЕВШАЯ И ДВУСМЫСЛЕННАЯ, оставлена до переноса
            -- показа (№28). В неё писались ДВА РАЗНЫХ ТЕГА ФФД по тернарнику:
            -- 1105 «сумма без НДС», если поле есть, иначе 1104 «сумма по ставке
            -- 0%». Чек целиком по ставке 0% при ndsNo=0 выглядел как чек без
            -- сумм: замер 28.08.2026, чек id=61 — 2670 ₽ пропали (№30).
            -- НЕ ЧИТАТЬ в новом коде. Дроп — после переноса показа и замера
            -- «чеков, где значение есть только здесь, — ноль», как в NDS-CLEANUP.
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS vat_0          NUMERIC(15,2);
            -- ДВЕ ВЕЛИЧИНЫ ВМЕСТО ОДНОЙ, И ОНИ БЫВАЮТ НЕПУСТЫ ОДНОВРЕМЕННО.
            -- Замер 28.08.2026 на чеке id=61: коды позиций [5,5,6,5,5,6,5,5,5] —
            -- в ОДНОМ чеке есть и ставка 0% (код 5), и «без НДС» (код 6).
            -- Одна колонка с признаком два числа не вместит: сложить — потерять
            -- различие, выбрать одно — повторить прежнюю ошибку буквально.
            --   sum_vat_0  — тег 1104, оборот по ставке 0% (операция облагается)
            --   sum_no_vat — тег 1105, оборот без НДС (освобождение, ст. 149 НК)
            -- Для покупателя это две РАЗНЫЕ причины отсутствия входящего НДС;
            -- в раздельном учёте (ст. 170 НК) и в выгрузке 1С — разные аналитики.
            -- Откат: ALTER TABLE receipts DROP COLUMN sum_vat_0, DROP COLUMN sum_no_vat;
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS sum_vat_0      NUMERIC(15,2);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS sum_no_vat     NUMERIC(15,2);
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS vat_breakdown  JSONB;
            -- NDS-CLEANUP ②: «НДС есть, ставка не распознана» — для фото-чеков.
            -- Распознавание видит суммы, а не коды ставок ФНС, поэтому раскладка
            -- по ставкам была домыслом; vat_20/vat_10 для этого пути больше
            -- не пишутся. У чеков ФНС здесь NULL: у них есть полная разбивка.
            -- Откат: ALTER TABLE receipts DROP COLUMN vat_total;
            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS vat_total      NUMERIC(15,2);
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
            CREATE INDEX IF NOT EXISTS idx_receipts_user_id         ON receipts(user_id);
            CREATE INDEX IF NOT EXISTS idx_receipt_items_receipt_id ON receipt_items(receipt_id);

            -- A-ACL backfill: старым чекам без автора проставляем первого админа их
            -- организации (по created_at). Идемпотентно — только user_id IS NULL.
            UPDATE receipts SET user_id = (
                SELECT u.id FROM users u
                WHERE u.org_id = receipts.org_id AND u.role='admin'
                ORDER BY u.created_at LIMIT 1
            ) WHERE user_id IS NULL;

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

        await _снять_колонки_ставок_ндс(conn)
        await _засеять_первого_администратора(conn)

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
