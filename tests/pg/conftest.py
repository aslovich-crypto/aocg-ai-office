# -*- coding: utf-8 -*-
"""Живой PostgreSQL для тестов (T36, пункт 2): настоящая база вместо FakePool.

ЗАЧЕМ. Двойник (tests/conftest.py) не раз оказывался ВЕРНЕЕ кода: зеркало
счёта админов держит `is_active` в ветке намертво (нарушение его же
«правила первого»), зеркала поиска и подсказки карты фильтровали то, что
обязан фильтровать SQL, — и мутацию в роутере пришлось учить ловить
ТЕКСТОМ запроса (М94/М95, Т136→T36). Класс дефекта один: «двойник делает
то, что проверяет тест», и снятое в роутере условие остаётся зелёным.
Настоящая база этого класса не имеет: SQL исполняется, а не толкуется.

КАК УСТРОЕНО.
  • Адрес базы — `TEST_DATABASE_URL` (так работает CI: сервис postgres:16,
    у pytest СВОЯ база, чтобы не пачкать пустую базу шага sql_check).
    Переменной нет — поднимается ВРЕМЕННЫЙ локальный кластер во временном
    каталоге (initdb + pg_ctl, только unix-сокет, TCP не открывается)
    и гасится после прогона. Приём отработан переездом S-06: временный
    кластер, не системная служба.
  • Нет ни переменной, ни бинарей — прогон ПАДАЕТ, а не пропускается:
    молчаливый скип — это «зелёный без прибора» (T87). Оба целевых
    окружения прибор имеют: этот Mac (homebrew postgresql@18) и CI.
  • Схему поднимает настоящий `init_db()` — тот же путь, что на старте
    контейнера. Отдельного тестового DDL нет и быть не должно: разойдясь,
    он стал бы вторым двойником.
  • Между тестами — TRUNCATE всех таблиц разом (список ИЗ САМОЙ базы,
    information_schema, а не из головы — забытая таблица не потечёт
    в соседний тест молча) + RESTART IDENTITY.

Фикстуры называются как у двойника (db/seeded/client/...) НАМЕРЕННО:
перевод теста с FakePool меняет строки работы с данными, а не сигнатуры.
Данные здесь правятся явными SQL-помощниками (`ЖиваяБаза`), а не
`db.users.append(...)`: список-атрибут — интерфейс двойника, у настоящей
базы интерфейс один — запрос.
"""

import asyncio
import os
import shutil
import subprocess
import tempfile

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import database
from app.auth import get_current_user
from app.database import _init_conn, init_db
from app.main import app

ИМЯ_БАЗЫ = "aocg_test"

# Куда смотреть за бинарями, когда их нет в PATH. Версионные каталоги
# СНАЧАЛА: `which initdb` на этой машине отвечает каталогом libpq,
# а серверные бинари (postgres, pg_ctl) лежат у postgresql@18 — pg_ctl
# обязан найти postgres РЯДОМ С СОБОЙ, иначе поднимет чужую версию.
_КАТАЛОГИ_PG = (
    "/opt/homebrew/opt/postgresql@18/bin",
    "/opt/homebrew/opt/postgresql@17/bin",
    "/opt/homebrew/opt/postgresql@16/bin",
    "/opt/homebrew/opt/postgresql/bin",
    "/usr/local/opt/postgresql@18/bin",
    "/usr/lib/postgresql/16/bin",
)


def _бинарь(имя):
    for каталог in _КАТАЛОГИ_PG:
        путь = os.path.join(каталог, имя)
        if os.path.exists(путь):
            return путь
    return shutil.which(имя)


@pytest.fixture(scope="session")
def адрес_живой_базы():
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if url:
        yield url
        return

    initdb = _бинарь("initdb")
    pg_ctl = _бинарь("pg_ctl")
    psql = _бинарь("psql")
    if not (initdb and pg_ctl and psql):
        pytest.fail(
            "ПРОВЕРКА НЕ ВЫПОЛНЕНА: живому контуру нужен PostgreSQL. "
            "Поставьте postgresql@18 (brew) или задайте TEST_DATABASE_URL. "
            "Скипа здесь нет намеренно: молчаливо-зелёный прогон без "
            "прибора хуже красного (T87)."
        )

    корень = tempfile.mkdtemp(prefix="aocgpg-")
    данные = os.path.join(корень, "d")
    # ⚠️ LC_ALL обязателен: без валидной локали постмастер PG18 на macOS
    # гаснет на старте с «postmaster became multithreaded during startup»
    # (замер 03.09.2026, лог кластера; HINT самого PostgreSQL — задать LC_ALL).
    окружение = {**os.environ, "LC_ALL": "C"}
    # builtin C.UTF-8 (PG17+): честная работа ILIKE/lower с кириллицей
    # без зависимости от локалей macOS. На старом PG — откат на чистый C:
    # для нынешних тестов регистр-кириллица не задействована, а честный
    # отказ initdb здесь хуже, чем суженная локаль.
    базовые = [
        initdb,
        "-D",
        данные,
        "-U",
        "postgres",
        "-A",
        "trust",
        "-E",
        "UTF8",
        "--no-sync",
        "--locale=C",
    ]
    р = subprocess.run(
        базовые + ["--locale-provider=builtin", "--builtin-locale=C.UTF-8"],
        capture_output=True,
        text=True,
        env=окружение,
    )
    if р.returncode != 0:
        shutil.rmtree(данные, ignore_errors=True)
        р = subprocess.run(базовые, capture_output=True, text=True, env=окружение)
    if р.returncode != 0:
        shutil.rmtree(корень, ignore_errors=True)
        pytest.fail(f"initdb не поднял кластер: {р.stderr[-1500:]}")

    р = subprocess.run(
        [
            pg_ctl,
            "-D",
            данные,
            "-l",
            os.path.join(корень, "log"),
            "-o",
            f"-c listen_addresses='' -c unix_socket_directories='{корень}' "
            "-c fsync=off",
            "start",
        ],
        capture_output=True,
        text=True,
        env=окружение,
    )
    if р.returncode != 0:
        shutil.rmtree(корень, ignore_errors=True)
        pytest.fail(f"pg_ctl не стартовал: {р.stderr[-1500:]}")

    try:
        р = subprocess.run(
            [
                psql,
                "-h",
                корень,
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                f"CREATE DATABASE {ИМЯ_БАЗЫ}",
            ],
            capture_output=True,
            text=True,
            env=окружение,
        )
        if р.returncode != 0:
            pytest.fail(f"CREATE DATABASE не прошёл: {р.stderr[-1500:]}")
        yield f"postgresql://postgres@/{ИМЯ_БАЗЫ}?host={корень}"
    finally:
        subprocess.run(
            [pg_ctl, "-D", данные, "-m", "immediate", "stop"],
            capture_output=True,
        )
        shutil.rmtree(корень, ignore_errors=True)


async def _поднять_схему(адрес):
    пул = await asyncpg.create_pool(адрес, init=_init_conn, min_size=1, max_size=2)
    database.pool = пул
    try:
        await init_db()
    finally:
        database.pool = None
        await пул.close()


@pytest.fixture(scope="session")
def живая_схема(адрес_живой_базы):
    """Схема поднята один раз на сессию — настоящим init_db().

    asyncio.run, а не async-фикстура: у pytest-asyncio петля на каждый тест
    своя, а пул asyncpg живёт только в родившей его петле. Схема — разовая
    работа, ей отдельная короткоживущая петля впору.
    """
    asyncio.run(_поднять_схему(адрес_живой_базы))
    return адрес_живой_базы


class ЖиваяБаза:
    """Явные SQL-помощники вместо списков двойника.

    id всюду передаётся ЯВНО (как в тестах на FakePool), поэтому после
    каждой вставки счётчик таблицы подводится setval-ом: иначе следующая
    вставка БЕЗ id (например, карта через POST) получит от последовательности
    занятый номер и упадёт по первичному ключу.
    """

    def __init__(self, pool):
        self.pool = pool

    async def _подвести_счётчик(self, таблица):
        await self.pool.execute(
            f"SELECT setval(pg_get_serial_sequence('{таблица}', 'id'), "
            f"(SELECT COALESCE(MAX(id), 1) FROM {таблица}))"
        )

    async def добавить_организацию(self, id, name="Тестовая"):
        await self.pool.execute(
            "INSERT INTO organizations (id, name, type) VALUES ($1, $2, 'company') "
            "ON CONFLICT (id) DO NOTHING",
            id,
            name,
        )
        await self._подвести_счётчик("organizations")

    async def добавить_пользователя(
        self,
        id,
        first_name=None,
        last_name=None,
        patronymic=None,
        email=None,
        role="employee",
        org_id=1,
        is_active=True,
        inn=None,
    ):
        # users.org_id — настоящий FK: организация обязана существовать.
        # Двойник этого не требовал; здесь строка без организации — дефект теста.
        await self.добавить_организацию(org_id)
        await self.pool.execute(
            "INSERT INTO users (id, first_name, last_name, patronymic, email, "
            "role, org_id, is_active, inn) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            id,
            first_name,
            last_name,
            patronymic,
            email,
            role,
            org_id,
            is_active,
            inn,
        )
        await self._подвести_счётчик("users")

    async def пользователь(self, id):
        строка = await self.pool.fetchrow("SELECT * FROM users WHERE id=$1", id)
        return dict(строка) if строка else None

    async def число_пользователей(self):
        return await self.pool.fetchval("SELECT count(*) FROM users")

    async def погасить(self, id):
        await self.pool.execute("UPDATE users SET is_active=false WHERE id=$1", id)

    async def добавить_карту(self, id, name, org_id=1):
        await self.pool.execute(
            "INSERT INTO cards (id, name, org_id) VALUES ($1, $2, $3)",
            id,
            name,
            org_id,
        )
        await self._подвести_счётчик("cards")

    async def карта(self, id):
        строка = await self.pool.fetchrow("SELECT * FROM cards WHERE id=$1", id)
        return dict(строка) if строка else None

    async def карты(self):
        return [
            dict(r) for r in await self.pool.fetch("SELECT * FROM cards ORDER BY id")
        ]

    async def число_приглашений(self):
        return await self.pool.fetchval("SELECT count(*) FROM invite_links")

    async def добавить_чек(
        self,
        id,
        org,
        amount,
        date,
        payment=None,
        kkt_fn=None,
        fd_num=None,
        org_id=1,
        user_id=None,
        org_brand=None,
        org_legal=None,
    ):
        await self.pool.execute(
            "INSERT INTO receipts (id, org, amount, date, payment, kkt_fn, fd_num, "
            "org_id, user_id, org_brand, org_legal, source) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'manual')",
            id,
            org,
            amount,
            date,
            payment,
            kkt_fn,
            fd_num,
            org_id,
            user_id,
            org_brand,
            org_legal,
        )
        await self._подвести_счётчик("receipts")

    async def чек(self, id):
        строка = await self.pool.fetchrow("SELECT * FROM receipts WHERE id=$1", id)
        return dict(строка) if строка else None

    async def чеки(self):
        return [
            dict(r) for r in await self.pool.fetch("SELECT * FROM receipts ORDER BY id")
        ]

    async def отчёт(self, id):
        строка = await self.pool.fetchrow("SELECT * FROM reports WHERE id=$1", id)
        return dict(строка) if строка else None

    async def добавить_отчёт(
        self, id, title, user_id, total=None, status="Черновик", org_id=1, created=None
    ):
        # user_id обязателен НАСТОЯЩИМ ограничением (reports.user_id NOT NULL,
        # REP-AUTHOR): у двойника отчёт без автора жил, здесь не живёт.
        await self.pool.execute(
            "INSERT INTO reports (id, title, status, total, org_id, user_id, created) "
            "VALUES ($1,$2,$3,$4,$5,$6, COALESCE($7, CURRENT_DATE))",
            id,
            title,
            status,
            total,
            org_id,
            user_id,
            created,
        )
        await self._подвести_счётчик("reports")

    async def положить_в_отчёт(self, report_id, receipt_id):
        """Связь чек→отчёт. На живой базе оба конца — настоящие FK, поэтому
        связь без отчёта или без чека здесь не заводится (у двойника заводилась,
        и тесты описывали состояние, которого не бывает)."""
        await self.pool.execute(
            "INSERT INTO report_items (report_id, receipt_id) VALUES ($1,$2)",
            report_id,
            receipt_id,
        )

    async def сумма_состава(self, report_id):
        """Сколько на самом деле лежит в отчёте — по чекам, а не по снимку."""
        return await self.pool.fetchval(
            "SELECT COALESCE(SUM(rc.amount), 0) FROM report_items ri "
            "JOIN receipts rc ON rc.id = ri.receipt_id WHERE ri.report_id = $1",
            report_id,
        )


@pytest_asyncio.fixture
async def db(живая_схема):
    """Чистая живая база на один тест: пул в петле теста, TRUNCATE до него."""
    пул = await asyncpg.create_pool(
        живая_схема, init=_init_conn, min_size=1, max_size=4
    )
    таблицы = await пул.fetchval(
        "SELECT string_agg(quote_ident(table_name), ', ') "
        "FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE'"
    )
    await пул.execute(f"TRUNCATE {таблицы} RESTART IDENTITY CASCADE")
    database.pool = пул
    yield ЖиваяБаза(пул)
    database.pool = None
    await пул.close()


@pytest_asyncio.fixture
async def seeded(db):
    """Та же основа, что у двойника: организация, Иван, карта, чек, отчёт."""
    from datetime import date

    await db.добавить_организацию(1, "АОЦГ")
    await db.добавить_пользователя(
        id=2,
        first_name="Иван",
        last_name="Петров",
        email="ivan@example.com",
        role="employee",
    )
    await db.добавить_карту(id=1, name="Корп.карта")
    await db.добавить_чек(
        id=1,
        org="Лукойл",
        amount=5000.0,
        date=date(2026, 5, 10),
        payment="Корп.карта",
        kkt_fn="FN-EXISTING-1",
    )
    await db.добавить_отчёт(
        id=1, title="Отчёт за май", user_id=2, total=5000.0, created=date(2026, 5, 10)
    )
    return db


def _подменить_смотрящего(role, user_id=1, org_id=1):
    # Копия _override_user из tests/conftest.py: тот же смотрящий, что у
    # двойника, — id=1, org_id=1, без JWT. Импортом не тянем, чтобы pg-контур
    # не зависел от модуля двойника, который пункт 3 T36 снесёт.
    app.dependency_overrides[get_current_user] = lambda: {
        "id": user_id,
        "org_id": org_id,
        "email": "test@aocg.ru",
        "first_name": "Test",
        "last_name": "User",
        "role": role,
        "is_email_verified": True,
        "password_hash": None,
    }


@pytest.fixture
def as_role():
    return _подменить_смотрящего


@pytest_asyncio.fixture
async def client(db):
    _подменить_смотрящего("admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client_accountant(db):
    _подменить_смотрящего("accountant")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client_employee(db):
    _подменить_смотрящего("employee", user_id=2)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
