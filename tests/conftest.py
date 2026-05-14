import os
import re
import sys
from datetime import date, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.database as database
from app.main import app


def _norm(q):
    return re.sub(r"\s+", " ", q).strip()


class FakePool:
    """In-memory stand-in for the asyncpg pool.

    No PostgreSQL is available in this environment (no Docker / brew / local
    server), so the suite runs against this fake. It implements exactly the
    queries the routers issue, so routing, validation and router business
    logic (409 dedup, FK cleanup on delete, payment suggestion) are exercised
    end-to-end without ever touching a real database.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.receipts = []
        self.reports = []
        self.report_items = []
        self.cards = []
        self._rid = self._repid = self._cid = 0

    async def fetch(self, query, *args):
        q = _norm(query)
        if q.startswith("SELECT * FROM receipts ORDER BY date DESC"):
            return sorted(self.receipts, key=lambda r: str(r["date"]), reverse=True)
        if q.startswith("SELECT * FROM reports ORDER BY created DESC"):
            return sorted(self.reports, key=lambda r: str(r["created"]), reverse=True)
        if q.startswith("SELECT * FROM report_items"):
            return list(self.report_items)
        if q.startswith("SELECT * FROM cards ORDER BY id"):
            return sorted(self.cards, key=lambda c: c["id"])
        if q.startswith("SELECT MIN(id)"):
            groups = {}
            for r in self.receipts:
                groups.setdefault((r["date"], r["amount"], r["org"]), []).append(r)
            return [
                dict(keep_id=min(x["id"] for x in rows), date=d, amount=a, org=o, cnt=len(rows))
                for (d, a, o), rows in groups.items() if len(rows) > 1
            ]
        raise NotImplementedError(f"fetch: {q}")

    async def fetchrow(self, query, *args):
        q = _norm(query)
        if q.startswith("SELECT payment FROM receipts WHERE org=$1"):
            counts = {}
            for r in self.receipts:
                if r["org"] == args[0] and r["payment"] and r["payment"] != "Не указано":
                    counts[r["payment"]] = counts.get(r["payment"], 0) + 1
            return {"payment": max(counts, key=counts.get)} if counts else None
        if q.startswith("SELECT * FROM receipts WHERE id=$1"):
            return next((dict(r) for r in self.receipts if r["id"] == args[0]), None)
        if q.startswith("SELECT id FROM receipts WHERE fn=$1"):
            return next(({"id": r["id"]} for r in self.receipts if r.get("fn") == args[0]), None)
        if q.startswith("INSERT INTO receipts"):
            self._rid += 1
            row = dict(id=self._rid, date=args[0], org=args[1], category=args[2],
                       payment=args[3], amount=args[4], employee=args[5],
                       fn=args[6], raw_data=args[7], created_at=datetime.utcnow())
            self.receipts.append(row)
            return dict(row)
        if q.startswith("UPDATE receipts SET"):
            set_part, where_part = q.split("SET", 1)[1].split("WHERE", 1)
            assignments = []
            for pair in set_part.split(","):
                m = re.match(r"\s*(\w+)\s*=\s*\$(\d+)", pair)
                assignments.append((m.group(1), int(m.group(2)) - 1))
            where_idx = int(re.search(r"\$(\d+)", where_part).group(1)) - 1
            rid = args[where_idx]
            for r in self.receipts:
                if r["id"] == rid:
                    for field, idx in assignments:
                        r[field] = args[idx]
                    return dict(r)
            return None
        if q.startswith("INSERT INTO reports"):
            self._repid += 1
            row = dict(id=self._repid, title=args[0], status="Личные", total=args[1],
                       created=date.today(), created_at=datetime.utcnow())
            self.reports.append(row)
            return dict(row)
        if q.startswith("UPDATE reports SET status=$1"):
            for r in self.reports:
                if r["id"] == args[1]:
                    r["status"] = args[0]
                    return dict(r)
            return None
        if q.startswith("INSERT INTO cards"):
            self._cid += 1
            row = dict(id=self._cid, name=args[0], created_at=datetime.utcnow())
            self.cards.append(row)
            return dict(row)
        if q.startswith("UPDATE cards SET name=$1"):
            for c in self.cards:
                if c["id"] == args[1]:
                    c["name"] = args[0]
                    return dict(c)
            return None
        raise NotImplementedError(f"fetchrow: {q}")

    async def execute(self, query, *args):
        q = _norm(query)
        if q.startswith(("CREATE TABLE", "ALTER TABLE", "CREATE UNIQUE INDEX")) \
           or "INSERT INTO cards (name) SELECT" in q:
            return "OK"
        if q.startswith("DELETE FROM receipts WHERE date=$1"):
            d, a, o, keep = args
            self.receipts = [r for r in self.receipts
                             if not (r["date"] == d and r["amount"] == a
                                     and r["org"] == o and r["id"] != keep)]
            return "DELETE"
        if q.startswith("DELETE FROM receipts WHERE id=$1"):
            self.receipts = [r for r in self.receipts if r["id"] != args[0]]
            return "DELETE"
        if q.startswith("DELETE FROM report_items WHERE receipt_id=$1"):
            self.report_items = [i for i in self.report_items if i["receipt_id"] != args[0]]
            return "DELETE"
        if q.startswith("INSERT INTO report_items"):
            self.report_items.append({"report_id": args[0], "receipt_id": args[1]})
            return "INSERT"
        if q.startswith("DELETE FROM cards WHERE id=$1"):
            self.cards = [c for c in self.cards if c["id"] != args[0]]
            return "DELETE"
        raise NotImplementedError(f"execute: {q}")

    def acquire(self):
        return _Acquire(self)


class _Acquire:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, q, *a):
        return await self.pool.execute(q, *a)

    async def fetch(self, q, *a):
        return await self.pool.fetch(q, *a)

    async def fetchrow(self, q, *a):
        return await self.pool.fetchrow(q, *a)

    def transaction(self):
        return _Txn()


class _Txn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def db():
    """Fresh in-memory pool wired into the app for one test, reset afterwards."""
    fake = FakePool()
    database.pool = fake
    yield fake
    fake.reset()
    database.pool = None


@pytest.fixture
def seeded(db):
    """Pre-populate baseline test data; cleaned up via the db fixture teardown."""
    now = datetime.utcnow()
    db.receipts.append(dict(id=1, date=date(2026, 5, 10), org="Лукойл", category="Топливо",
                            payment="Корп.карта", amount=5000.0, employee=None,
                            fn="FN-EXISTING-1", raw_data=None, created_at=now))
    db._rid = 1
    db.cards.append(dict(id=1, name="Корп.карта", created_at=now))
    db._cid = 1
    db.reports.append(dict(id=1, title="Отчёт за май", status="Личные", total=5000.0,
                           created=date(2026, 5, 10), created_at=now))
    db._repid = 1
    return db


@pytest_asyncio.fixture
async def client(db):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
