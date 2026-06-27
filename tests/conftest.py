import os
import re
import sys
from datetime import date, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# auth.py теперь требует JWT_SECRET_KEY (fail-fast, без небезопасного дефолта).
# Тестам нужен любой непустой ключ — ставим ДО импорта приложения ниже.
os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-used-in-prod")

# AOCGSecurityMiddleware читает env при импорте app.main. В тестах: выключаем
# enforce-HTTPS (TestClient ходит по http) и поднимаем лимиты, чтобы middleware
# был АКТИВЕН, но не валил тесты 403/429. setdefault — НЕ перетирает, если
# переменные уже заданы в окружении (можно прогнать с боевыми лимитами явно).
# Прод-дефолты (60/5, enforce_https=true) тут НЕ меняются — это только тест-env.
os.environ.setdefault("SECURITY_ENFORCE_HTTPS", "false")
os.environ.setdefault("SECURITY_RATE_LIMIT", "100000")
os.environ.setdefault("SECURITY_AUTH_RATE_LIMIT", "100000")

import app.database as database
from app.auth import get_current_user
from app.main import app


def _norm(q):
    return re.sub(r"\s+", " ", q).strip()


def _dup_fakerow(r, pool):
    """Строка для дедуп-веток 2/3 (фаза C): то, что отдаёт расширенный SELECT —
    id/org/amount/date/source/kkt_fn + in_report (EXISTS report_items)."""
    return {
        "id": r["id"],
        "org": r["org"],
        "amount": r["amount"],
        "date": r["date"],
        "source": r.get("source"),
        "kkt_fn": r.get("kkt_fn"),
        "in_report": any(ri["receipt_id"] == r["id"] for ri in pool.report_items),
    }


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
        self.receipt_items = []
        self.reports = []
        self.report_items = []
        self.cards = []
        self.consents = []
        self.category_groups = []  # Фикс №1 фаза A: справочник категорий (per-org)
        self.categories = []
        self.organizations = []  # Задача #1: профиль организации
        self._rid = self._repid = self._cid = self._consid = 0
        self._gid = self._catid = 0

    async def fetchval(self, query, *args):
        # Фикс №1 фаза A: seed_default_categories использует fetchval для idempotency-
        # проверки и для INSERT ... RETURNING id групп.
        q = _norm(query)
        if q.startswith("SELECT EXISTS(SELECT 1 FROM categories WHERE org_id=$1"):
            return any(c.get("org_id") == args[0] for c in self.categories)
        if q.startswith("INSERT INTO category_groups"):
            self._gid += 1
            self.category_groups.append(
                {
                    "id": self._gid,
                    "org_id": args[0],
                    "name": args[1],
                    "position": args[2],
                }
            )
            return self._gid
        if "COALESCE(MAX(position),0)+1 FROM categories" in q:
            # Фаза C POST: следующая position статьи внутри группы (per-org).
            group_id, org_id = args
            positions = [
                c["position"]
                for c in self.categories
                if c.get("group_id") == group_id and c.get("org_id") == org_id
            ]
            return (max(positions) if positions else 0) + 1
        if q.startswith("SELECT COUNT(*) FROM receipts WHERE category_id=$1"):
            # Фаза C DELETE-гард: сколько чеков привязано к категории (per-org).
            cat_id, org_id = args
            return sum(
                1
                for r in self.receipts
                if r.get("category_id") == cat_id and r.get("org_id") == org_id
            )
        raise NotImplementedError(f"fetchval: {q}")

    async def fetch(self, query, *args):
        q = _norm(query)
        if q.startswith("SELECT * FROM receipts WHERE org_id=$1 ORDER BY date DESC"):
            return sorted(
                [r for r in self.receipts if r.get("org_id") == args[0]],
                key=lambda r: str(r["date"]),
                reverse=True,
            )
        if q.startswith(
            "SELECT * FROM receipts WHERE org_id=$1 AND user_id=$2 ORDER BY date DESC"
        ):
            # A-ACL: employee видит только свои чеки (org_id + автор).
            return sorted(
                [
                    r
                    for r in self.receipts
                    if r.get("org_id") == args[0] and r.get("user_id") == args[1]
                ],
                key=lambda r: str(r["date"]),
                reverse=True,
            )
        if q.startswith("SELECT * FROM reports WHERE org_id=$1 ORDER BY created DESC"):
            return sorted(
                [r for r in self.reports if r.get("org_id") == args[0]],
                key=lambda r: str(r["created"]),
                reverse=True,
            )
        if q.startswith("SELECT ri.* FROM report_items"):
            return list(self.report_items)
        if q.startswith("SELECT * FROM cards WHERE org_id=$1 ORDER BY id"):
            return sorted(
                [c for c in self.cards if c.get("org_id") == args[0]],
                key=lambda c: c["id"],
            )
        if q.startswith(
            "SELECT id, name, position FROM category_groups WHERE org_id=$1"
        ):
            # Фаза C GET: группы орг по position.
            return sorted(
                [g for g in self.category_groups if g.get("org_id") == args[0]],
                key=lambda g: (g["position"], g["id"]),
            )
        if q.startswith(
            "SELECT id, group_id, name, tax_kind, position, is_default, is_visible"
        ):
            # Фаза C GET: все статьи орг по position (фильтр visible_only — на стороне роутера).
            return sorted(
                [dict(c) for c in self.categories if c.get("org_id") == args[0]],
                key=lambda c: (c["position"], c["id"]),
            )
        if q.startswith("SELECT MIN(id)"):
            groups = {}
            for r in self.receipts:
                groups.setdefault((r["date"], r["amount"], r["org"]), []).append(r)
            return [
                dict(
                    keep_id=min(x["id"] for x in rows),
                    date=d,
                    amount=a,
                    org=o,
                    cnt=len(rows),
                )
                for (d, a, o), rows in groups.items()
                if len(rows) > 1
            ]
        if q.startswith("SELECT id FROM receipts WHERE id = ANY($1"):
            # S-15 IDOR-проверка создания отчёта: какие из запрошенных id реально
            # принадлежат орг пользователя (чужие/несуществующие сюда не попадут).
            ids, org_id = args
            return [
                {"id": r["id"]}
                for r in self.receipts
                if r["id"] in ids and r.get("org_id") == org_id
            ]
        if "id = ANY($1" in q and "EXISTS" in q:
            # Bulk-delete кандидаты (фаза C): чеки своей орг из списка id + in_report.
            # Чужие id не попадают в выборку (изоляция по org_id).
            # A-ACL: для не-admin добавляется user_id=$3 — чужие по автору отсеиваются.
            ids = args[0]
            org_id = args[1]
            user_id = args[2] if len(args) > 2 else None
            return [
                {
                    "id": r["id"],
                    "kkt_fn": r.get("kkt_fn"),
                    "in_report": any(
                        ri["receipt_id"] == r["id"] for ri in self.report_items
                    ),
                }
                for r in self.receipts
                if r["id"] in ids
                and r.get("org_id") == org_id
                and (user_id is None or r.get("user_id") == user_id)
            ]
        if "org_inn = $3" in q and "7 days" in q:
            # Ветка 2 — сильный composite (фаза C): ВСЕ совпадения, created_at ASC.
            # Динамический fn-фильтр: при has_reliable_fn матчим только fn-less чеки.
            d, amount, org_inn, org_id, has_reliable_fn = args
            cutoff = datetime.utcnow() - timedelta(days=7)
            hits = [
                r
                for r in self.receipts
                if (
                    r["date"] == d
                    and r["amount"] == amount
                    and r.get("org_inn") == org_inn
                    and r.get("org_id") == org_id
                    and (not has_reliable_fn or r.get("kkt_fn") is None)
                    and r.get("created_at")
                    and r["created_at"] > cutoff
                )
            ]
            hits.sort(key=lambda r: r["created_at"])
            return [_dup_fakerow(r, self) for r in hits]
        if "amount = $2 AND org_id = $3 AND (NOT $4" in q and "7 days" in q:
            # Ветка 3 — слабый composite (фаза C): ВСЕ совпадения, created_at ASC.
            d, amount, org_id, has_reliable_fn = args
            cutoff = datetime.utcnow() - timedelta(days=7)
            hits = [
                r
                for r in self.receipts
                if (
                    r["date"] == d
                    and r["amount"] == amount
                    and r.get("org_id") == org_id
                    and (not has_reliable_fn or r.get("kkt_fn") is None)
                    and r.get("created_at")
                    and r["created_at"] > cutoff
                )
            ]
            hits.sort(key=lambda r: r["created_at"])
            return [_dup_fakerow(r, self) for r in hits]
        raise NotImplementedError(f"fetch: {q}")

    async def fetchrow(self, query, *args):
        q = _norm(query)
        # Задача #1/INT: профиль организации (GET /api/organizations/me).
        if q.startswith(
            "SELECT id, name, inn, type, owner_id, created_at, tax_system FROM organizations"
        ):
            return next(
                (dict(o) for o in self.organizations if o["id"] == args[0]), None
            )
        # auth-payload _org(): краткий профиль для ответа логина/me.
        if q.startswith("SELECT id, name, inn, type FROM organizations WHERE id=$1"):
            o = next((x for x in self.organizations if x["id"] == args[0]), None)
            return {k: o.get(k) for k in ("id", "name", "inn", "type")} if o else None
        # Задача #1/INT: правка профиля (PATCH) — COALESCE сохраняет текущее при None.
        if q.startswith("UPDATE organizations SET name=COALESCE($1,name)"):
            new_name, new_inn, new_tax, org_id = args
            o = next((x for x in self.organizations if x["id"] == org_id), None)
            if not o:
                return None
            if new_name is not None:
                o["name"] = new_name
            if new_inn is not None:
                o["inn"] = new_inn
            if new_tax is not None:
                o["tax_system"] = new_tax
            return dict(o)
        if q.startswith("SELECT payment FROM receipts WHERE org=$1"):
            counts = {}
            for r in self.receipts:
                if (
                    r["org"] == args[0]
                    and r["payment"]
                    and r["payment"] != "Не указано"
                ):
                    counts[r["payment"]] = counts.get(r["payment"], 0) + 1
            return {"payment": max(counts, key=counts.get)} if counts else None
        if q.startswith("SELECT * FROM receipts WHERE id=$1"):
            # A-ACL: enforce org_id (и user_id для employee), а не только id.
            rid = args[0]
            org_id = args[1] if len(args) > 1 else None
            user_id = args[2] if len(args) > 2 else None
            return next(
                (
                    dict(r)
                    for r in self.receipts
                    if r["id"] == rid
                    and (org_id is None or r.get("org_id") == org_id)
                    and (user_id is None or r.get("user_id") == user_id)
                ),
                None,
            )
        if q.startswith("SELECT photo_url, raw_data FROM receipts WHERE id=$1"):
            # A-ACL: enforce org_id (и user_id для employee).
            rid = args[0]
            org_id = args[1] if len(args) > 1 else None
            user_id = args[2] if len(args) > 2 else None
            r = next(
                (
                    x
                    for x in self.receipts
                    if x["id"] == rid
                    and (org_id is None or x.get("org_id") == org_id)
                    and (user_id is None or x.get("user_id") == user_id)
                ),
                None,
            )
            return (
                dict(photo_url=r.get("photo_url"), raw_data=r.get("raw_data"))
                if r
                else None
            )
        if q.startswith("SELECT id FROM receipts WHERE kkt_fn=$1 AND fd_num=$2"):
            # Дедуп по документу — per-org, пара (kkt_fn=ФН, fd_num=ФД):
            # WHERE kkt_fn=$1 AND fd_num=$2 AND org_id=$3.
            return next(
                (
                    {"id": r["id"]}
                    for r in self.receipts
                    if r.get("kkt_fn") == args[0]
                    and r.get("fd_num") == args[1]
                    and r.get("org_id") == args[2]
                ),
                None,
            )
        if q.startswith("SELECT id FROM categories WHERE org_id=$1 AND name=$2"):
            # Фикс №1 фаза B: resolve_category_id — имя статьи → id per-org.
            return next(
                (
                    {"id": c["id"]}
                    for c in self.categories
                    if c.get("org_id") == args[0] and c.get("name") == args[1]
                ),
                None,
            )
        if q.startswith("SELECT id FROM category_groups WHERE id=$1 AND org_id=$2"):
            # Фаза C POST: группа принадлежит орг? (защита от чужого group_id).
            return next(
                (
                    {"id": g["id"]}
                    for g in self.category_groups
                    if g["id"] == args[0] and g.get("org_id") == args[1]
                ),
                None,
            )
        if q.startswith(
            "SELECT id, is_default FROM categories WHERE id=$1 AND org_id=$2"
        ):
            # Фаза C PATCH/DELETE: существует + системная ли (per-org).
            return next(
                (
                    {"id": c["id"], "is_default": c.get("is_default", True)}
                    for c in self.categories
                    if c["id"] == args[0] and c.get("org_id") == args[1]
                ),
                None,
            )
        if q.startswith("INSERT INTO categories") and "RETURNING" in q:
            # Фаза C POST: пользовательская статья (is_default=FALSE). UNIQUE(org_id,name).
            org_id, group_id, name, tax_kind, position = args[:5]
            if any(
                c.get("org_id") == org_id and c.get("name") == name
                for c in self.categories
            ):
                raise asyncpg.exceptions.UniqueViolationError(
                    'duplicate key value violates unique constraint "categories_org_id_name_key"'
                )
            self._catid += 1
            row = {
                "id": self._catid,
                "org_id": org_id,
                "group_id": group_id,
                "name": name,
                "tax_kind": tax_kind,
                "position": position,
                "is_default": False,
                "is_visible": True,
            }
            self.categories.append(row)
            return dict(row)
        if q.startswith("UPDATE categories SET"):
            # Фаза C PATCH (name/tax_kind) и visibility — общий динамический матчер.
            set_part, where_part = q.split("SET", 1)[1].split("WHERE", 1)
            assignments = []
            for pair in set_part.split(","):
                m = re.match(r"\s*(\w+)\s*=\s*\$(\d+)", pair)
                assignments.append((m.group(1), int(m.group(2)) - 1))
            idxs = [int(x) - 1 for x in re.findall(r"\$(\d+)", where_part)]
            cat_id, org_id = args[idxs[0]], args[idxs[1]]
            target = next(
                (
                    c
                    for c in self.categories
                    if c["id"] == cat_id and c.get("org_id") == org_id
                ),
                None,
            )
            if not target:
                return None
            for field, idx in assignments:
                if field == "name" and any(
                    c["id"] != cat_id
                    and c.get("org_id") == org_id
                    and c.get("name") == args[idx]
                    for c in self.categories
                ):
                    raise asyncpg.exceptions.UniqueViolationError(
                        'duplicate key value violates unique constraint "categories_org_id_name_key"'
                    )
            for field, idx in assignments:
                target[field] = args[idx]
            return dict(target)
        if "AND source = $4" in q and "90 seconds" in q:
            # Ветка 0 — двойной тап: date+amount+org_id+source, fn-less, окно 90 сек.
            d, amount, org_id, source = args
            cutoff = datetime.utcnow() - timedelta(seconds=90)
            for r in self.receipts:
                if (
                    r["date"] == d
                    and r["amount"] == amount
                    and r.get("org_id") == org_id
                    and r.get("source") == source
                    and r.get("kkt_fn") is None
                    and r.get("created_at")
                    and r["created_at"] > cutoff
                ):
                    return {"id": r["id"]}
            return None
        # NB: дедуп-ветки 2/3 (composite, окно 7 дней) с фазы C идут через fetch
        # (массив duplicates), а не fetchrow — их матчеры в методе fetch ниже.
        if q.startswith("INSERT INTO receipts"):
            # 31-arg insert (org_id, date, … , category_id, user_id — автор чека, A-ACL).
            # Зеркалит порядок колонок в receipts.py. card_id не вставляется → None.
            args = list(args) + [None] * (31 - len(args))
            kkt_fn_val = args[6]
            fd_num_val = args[26]
            # Mirror the GLOBAL partial-unique index receipts_kkt_fn_fd_unique:
            # a (kkt_fn, fd_num) pair (both non-NULL) already present (in ANY org)
            # → UniqueViolationError. ФН в одиночку больше дубль НЕ образует.
            if (
                kkt_fn_val is not None
                and fd_num_val is not None
                and any(
                    r.get("kkt_fn") == kkt_fn_val and r.get("fd_num") == fd_num_val
                    for r in self.receipts
                )
            ):
                raise asyncpg.exceptions.UniqueViolationError(
                    'duplicate key value violates unique constraint "receipts_kkt_fn_fd_unique"'
                )
            self._rid += 1
            row = dict(
                id=self._rid,
                org_id=args[0],
                date=args[1],
                org=args[2],
                payment=args[3],
                amount=args[4],
                employee=args[5],
                kkt_fn=args[6],
                raw_data=args[7],
                source=args[8] or "manual",
                photo_url=args[9],
                datetime=args[10],
                currency=args[11],
                operation_type=args[12],
                org_legal=args[13],
                org_brand=args[14],
                org_inn=args[15],
                payment_form=args[16],
                payment_detail=args[17],
                card_last4=args[18],
                tax_system=args[19],
                address=args[20],
                vat_20=args[21],
                vat_10=args[22],
                vat_0=args[23],
                kkt_serial=args[24],
                kkt_rn=args[25],
                fd_num=args[26],
                fpd=args[27],
                cashier=args[28],
                category_id=args[29],
                user_id=args[30],
                card_id=None,
                created_at=datetime.utcnow(),
            )
            self.receipts.append(row)
            return dict(row)
        if q.startswith("UPDATE receipts SET"):
            set_part, where_part = q.split("SET", 1)[1].split("WHERE", 1)
            assignments = []
            for pair in set_part.split(","):
                m = re.match(r"\s*(\w+)\s*=\s*\$(\d+)", pair)
                assignments.append((m.group(1), int(m.group(2)) - 1))
            # A-ACL: honor ВСЕ условия WHERE (id, org_id и — для employee — user_id),
            # а не только id (раньше org_id игнорировался).
            where_conds = [
                (mm.group(1), int(mm.group(2)) - 1)
                for mm in re.finditer(r"(\w+)=\$(\d+)", where_part)
            ]
            for r in self.receipts:
                if all(r.get(col) == args[idx] for col, idx in where_conds):
                    for field, idx in assignments:
                        r[field] = args[idx]
                    return dict(r)
            return None
        if q.startswith("INSERT INTO reports"):
            self._repid += 1
            row = dict(
                id=self._repid,
                title=args[0],
                status="Личные",
                total=args[1],
                org_id=args[2] if len(args) > 2 else None,
                created=date.today(),
                created_at=datetime.utcnow(),
            )
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
            row = dict(
                id=self._cid,
                name=args[0],
                org_id=args[1] if len(args) > 1 else None,
                created_at=datetime.utcnow(),
            )
            self.cards.append(row)
            return dict(row)
        if q.startswith("UPDATE cards SET name=$1"):
            for c in self.cards:
                if c["id"] == args[1]:
                    c["name"] = args[0]
                    return dict(c)
            return None
        if q.startswith("INSERT INTO user_consents"):
            self._consid += 1
            row = dict(
                id=self._consid,
                user_id=args[0],
                ip_address=args[1],
                policy_version=args[2],
                consent_text=args[3],
                consent_at=datetime.utcnow(),
            )
            self.consents.append(row)
            return dict(row)
        if q.startswith("SELECT id, consent_at, policy_version FROM user_consents"):
            matches = [c for c in self.consents if c["user_id"] == args[0]]
            if not matches:
                return None
            latest = max(matches, key=lambda c: c["consent_at"])
            return dict(latest)
        raise NotImplementedError(f"fetchrow: {q}")

    async def execute(self, query, *args):
        q = _norm(query)
        if (
            q.startswith(
                ("CREATE TABLE", "ALTER TABLE", "CREATE UNIQUE INDEX", "CREATE INDEX")
            )
            or "INSERT INTO cards (name) SELECT" in q
        ):
            return "OK"
        if q.startswith("DELETE FROM receipts WHERE date=$1"):
            d, a, o, keep = args
            self.receipts = [
                r
                for r in self.receipts
                if not (
                    r["date"] == d
                    and r["amount"] == a
                    and r["org"] == o
                    and r["id"] != keep
                )
            ]
            return "DELETE"
        if q.startswith(
            "DELETE FROM receipts WHERE id=$1 AND org_id=$2 AND user_id=$3"
        ):
            # A-ACL: employee/accountant удаляют только свой чек (по автору).
            rid, org_id, user_id = args
            self.receipts = [
                r
                for r in self.receipts
                if not (
                    r["id"] == rid
                    and r.get("org_id") == org_id
                    and r.get("user_id") == user_id
                )
            ]
            return "DELETE"
        if q.startswith("DELETE FROM receipts WHERE id=$1 AND org_id=$2"):
            # Одиночный DELETE: только чек своей орг (фикс P1 — был фильтр лишь по id).
            rid, org_id = args
            self.receipts = [
                r
                for r in self.receipts
                if not (r["id"] == rid and r.get("org_id") == org_id)
            ]
            return "DELETE"
        if q.startswith(
            "DELETE FROM report_items WHERE receipt_id=$1 AND receipt_id IN"
        ):
            # Одиночный DELETE (фикс P1): org-безопасно — только связь чека СВОЕЙ орг.
            # A-ACL: для не-admin добавляется user_id=$3 (только связи своих чеков).
            rid = args[0]
            org_id = args[1]
            user_id = args[2] if len(args) > 2 else None
            own = {
                r["id"]
                for r in self.receipts
                if r.get("org_id") == org_id
                and (user_id is None or r.get("user_id") == user_id)
            }
            self.report_items = [
                ri
                for ri in self.report_items
                if not (ri["receipt_id"] == rid and ri["receipt_id"] in own)
            ]
            return "DELETE"
        if q.startswith("INSERT INTO report_items"):
            self.report_items.append({"report_id": args[0], "receipt_id": args[1]})
            return "INSERT"
        if q.startswith("INSERT INTO receipt_items"):
            self.receipt_items.append(
                {
                    "receipt_id": args[0],
                    "position": args[1],
                    "name": args[2],
                    "quantity": args[3],
                    "price": args[4],
                    "sum": args[5],
                    "vat_rate": args[6],
                }
            )
            return "INSERT"
        if q.startswith("DELETE FROM cards WHERE id=$1"):
            self.cards = [c for c in self.cards if c["id"] != args[0]]
            return "DELETE"
        if q.startswith("DELETE FROM categories WHERE id=$1 AND org_id=$2"):
            # Фаза C DELETE: только своя орг (роутер уже проверил is_default и чеки).
            cat_id, org_id = args
            self.categories = [
                c
                for c in self.categories
                if not (c["id"] == cat_id and c.get("org_id") == org_id)
            ]
            return "DELETE"
        if q.startswith("DELETE FROM report_items WHERE receipt_id = ANY($1"):
            # Bulk (фаза C): org-безопасно — только связи чеков СВОЕЙ орг.
            ids, org_id = args
            own = {r["id"] for r in self.receipts if r.get("org_id") == org_id}
            self.report_items = [
                ri
                for ri in self.report_items
                if not (ri["receipt_id"] in ids and ri["receipt_id"] in own)
            ]
            return "DELETE"
        if q.startswith("DELETE FROM receipts WHERE id = ANY($1"):
            ids, org_id = args
            self.receipts = [
                r
                for r in self.receipts
                if not (r["id"] in ids and r.get("org_id") == org_id)
            ]
            return "DELETE"
        if q.startswith("INSERT INTO categories"):
            # Фикс №1 фаза A: статья справочника (group_id из fetchval выше).
            self._catid += 1
            self.categories.append(
                {
                    "id": self._catid,
                    "org_id": args[0],
                    "group_id": args[1],
                    "name": args[2],
                    "tax_kind": args[3],
                    "position": args[4],
                    "is_default": True,
                    "is_visible": True,
                }
            )
            return "INSERT"
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

    async def fetchval(self, q, *a):
        return await self.pool.fetchval(q, *a)

    def transaction(self):
        return _Txn(self.pool)


class _Txn:
    # Реальный asyncpg откатывает транзакцию при исключении. Раньше FakePool
    # этого не делал — тесты на rollback были слепы (S-15). Снимаем снапшот
    # состояния на входе и восстанавливаем на выходе, если поднялось исключение.
    _LISTS = (
        "receipts",
        "receipt_items",
        "reports",
        "report_items",
        "cards",
        "consents",
        "category_groups",
        "categories",
    )
    _COUNTERS = ("_rid", "_repid", "_cid", "_consid", "_gid", "_catid")

    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        self._snap_lists = {n: list(getattr(self.pool, n)) for n in self._LISTS}
        self._snap_counters = {n: getattr(self.pool, n) for n in self._COUNTERS}
        return self

    async def __aexit__(self, exc_type, *rest):
        if exc_type is not None:  # исключение → откат к снапшоту
            for n, val in self._snap_lists.items():
                setattr(self.pool, n, list(val))
            for n, val in self._snap_counters.items():
                setattr(self.pool, n, val)
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
    db.receipts.append(
        dict(
            id=1,
            date=date(2026, 5, 10),
            org="Лукойл",
            payment="Корп.карта",
            amount=5000.0,
            employee=None,
            kkt_fn="FN-EXISTING-1",
            raw_data=None,
            source="manual",
            photo_url=None,
            org_id=1,
            created_at=now,
        )
    )
    db._rid = 1
    db.cards.append(dict(id=1, name="Корп.карта", org_id=1, created_at=now))
    db._cid = 1
    db.reports.append(
        dict(
            id=1,
            title="Отчёт за май",
            status="Личные",
            total=5000.0,
            org_id=1,
            created=date(2026, 5, 10),
            created_at=now,
        )
    )
    db._repid = 1
    return db


def _override_user(role, user_id=1):
    """Подменяет get_current_user фиксированным юзером org_id=1 с заданной ролью."""
    app.dependency_overrides[get_current_user] = lambda: {
        "id": user_id,
        "org_id": 1,
        "email": "test@aocg.ru",
        "first_name": "Test",
        "last_name": "User",
        "role": role,
        "is_email_verified": True,
        "password_hash": None,
    }


@pytest_asyncio.fixture
async def client(db):
    # Routers now depend on get_current_user (auth + org scoping). Tests don't
    # carry a JWT, so override the dependency with a fixed org_id=1 user.
    _override_user("admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client_accountant(db):
    """Фаза C: бухгалтер — может мутировать категории (вариант 1)."""
    _override_user("accountant")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client_employee(db):
    """Фаза C: сотрудник — GET работает, мутации → 403."""
    _override_user("employee", user_id=2)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
