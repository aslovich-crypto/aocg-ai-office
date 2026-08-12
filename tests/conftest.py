import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

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


def _with_in_report(r, pool):
    """Строка чека + in_report / report_id / report_title (ЧП4а).

    Зеркалит LEFT JOIN report_items→reports в SELECT списка и деталей чека,
    а также в развёрнутых чеках деталей отчёта. Связь 1:0..1 держит уникальный
    индекс uq_report_items_receipt_id — поэтому берём первое совпадение.
    """
    d = dict(r)
    link = next((ri for ri in pool.report_items if ri["receipt_id"] == r["id"]), None)
    rep = (
        next((x for x in pool.reports if x["id"] == link["report_id"]), None)
        if link
        else None
    )
    d["in_report"] = link is not None
    d["report_id"] = rep["id"] if rep else None
    d["report_title"] = rep["title"] if rep else None
    return d


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

    ПРАВИЛО ПЕРВОЕ: ДВОЙНИК НЕ ДЕЛАЕТ ТОГО, ЧТО ПРОВЕРЯЕТ ТЕСТ.
    Условие, которое тест обязан подтвердить (org-scope, фильтр автора,
    is_active), НЕЛЬЗЯ зашивать в ветку намертво — его надо ВЫВОДИТЬ
    ИЗ ТЕКСТА ЗАПРОСА:

        свой = "AND org_id=$2" in q
        ... if not свой or строка["org_id"] == args[1]

    Зашитый фильтр делает тест ВЕЧНОЗЕЛЁНЫМ и молча: убери условие
    из роутера — фильтровать продолжит двойник, прогон останется зелёным,
    а в проде откроется чужая организация. Это не теория: ветка удаления
    карты фильтровала по одному id, игнорируя org_id (то есть была
    ПОЗВОЛИТЕЛЬНЕЕ продакшена), и тест «чужую карту не удалили» с ней
    не мог бы ни пройти честно, ни покраснеть на снятом org-scope.
    Обратный случай — ветка приглашений, где зашитый scope дал бы
    вечнозелёный тест. Оба найдены 07.08.2026 (S-31).

    Проверка та же, что у сторожей (T11): снять условие в РОУТЕРЕ
    и убедиться, что тест краснеет. Не краснеет — фильтрует двойник.

    ПРАВИЛО ВТОРОЕ: порядок веток = приоритет, первое совпадение
    выигрывает. Общий шаблон выше специфичного убивает специфичный
    молча — за этим следит сторож `tests/test_fakepool_order.py` (T6).
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
        # S-29: мутации users до 07.08.2026 не покрывались тестами вовсе,
        # поэтому и таблицы здесь не было. Ветки ниже зеркалят SQL роутера.
        self.users = []
        # S-31: приглашения и отозванные токены — контур выдачи и отзыва
        # доступа. Не выполнялся в тестах ни разу (замер tests/tools).
        self.invite_links = []
        self.revoked_tokens = []
        self._invid = 0
        self._uid = 0
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
        # ЧП4а: списки чеков теперь тянут вычисляемый in_report (EXISTS по
        # report_items), поэтому запрос начинается с «SELECT *, EXISTS(…)».
        # Различаем ветки по хвосту WHERE — префиксы у них общие.
        if "WHERE receipts.org_id=$1 ORDER BY receipts.date DESC" in q:
            return sorted(
                [
                    _with_in_report(r, self)
                    for r in self.receipts
                    if r.get("org_id") == args[0]
                ],
                key=lambda r: str(r["date"]),
                reverse=True,
            )
        if "WHERE receipts.org_id=$1 AND receipts.user_id=$2" in q:
            # A-ACL: employee видит только свои чеки (org_id + автор).
            return sorted(
                [
                    _with_in_report(r, self)
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
        # REP-ACL: сотрудник видит только свои отчёты (org + автор).
        if q.startswith(
            "SELECT * FROM reports WHERE org_id=$1 AND user_id=$2 ORDER BY created DESC"
        ):
            return sorted(
                [
                    r
                    for r in self.reports
                    if r.get("org_id") == args[0] and r.get("user_id") == args[1]
                ],
                key=lambda r: str(r["created"]),
                reverse=True,
            )
        if q.startswith(
            "SELECT ri.* FROM report_items ri JOIN reports r ON r.id = ri.report_id "
            "WHERE r.org_id=$1 AND r.user_id=$2"
        ):
            own = {
                r["id"]
                for r in self.reports
                if r.get("org_id") == args[0] and r.get("user_id") == args[1]
            }
            return [ri for ri in self.report_items if ri["report_id"] in own]
        if q.startswith("SELECT ri.* FROM report_items"):
            # T35: JOIN с reports по org БЕРЁТСЯ ИЗ ТЕКСТА. Раньше ветка
            # отдавала состав ВСЕХ отчётов всех организаций.
            свой = "r.org_id=$1" in q
            свои_отчёты = {
                r["id"] for r in self.reports if not свой or r.get("org_id") == args[0]
            }
            return [ri for ri in self.report_items if ri["report_id"] in свои_отчёты]
        if q.startswith("SELECT receipt_id FROM report_items WHERE report_id=$1"):
            return sorted(
                [ri for ri in self.report_items if ri["report_id"] == args[0]],
                key=lambda ri: ri["receipt_id"],
            )
        if q.startswith("SELECT * FROM cards WHERE org_id=$1 ORDER BY id"):
            return sorted(
                [c for c in self.cards if c.get("org_id") == args[0]],
                key=lambda c: c["id"],
            )
        if q.startswith(
            "SELECT MIN(id) AS keep_id, date, amount, org, COUNT(*) AS cnt"
        ):
            # S-31: группировка дублей для dedupe-cleanup. АГРЕГАТ ЭМУЛИРУЕТСЯ
            # ВРУЧНУЮ — тест на этой ветке доказывает логику ХЕНДЛЕРА (цикл,
            # счётчики, org-scope в удалении), а не то, что GROUP BY в SQL
            # написан верно. Настоящий PostgreSQL — задача T36.
            свой = "org_id=$1" in q
            группы = {}
            for r in self.receipts:
                if свой and r.get("org_id") != args[0]:
                    continue
                ключ = (r["date"], r["amount"], r["org"])
                группы.setdefault(ключ, []).append(r["id"])
            return [
                {
                    "keep_id": min(ids),
                    "date": k[0],
                    "amount": k[1],
                    "org": k[2],
                    "cnt": len(ids),
                }
                for k, ids in группы.items()
                if len(ids) > 1
            ]
        if q.startswith("SELECT * FROM invite_links"):
            # S-31: список приглашений. ФИЛЬТРЫ БЕРУТСЯ ИЗ ТЕКСТА ЗАПРОСА,
            # а не зашиты: зашитый org-scope сделал бы тест «не вижу чужие
            # приглашения» ВЕЧНОЗЕЛЁНЫМ — фильтровал бы FakePool, даже если
            # из роутера убрать «AND org_id=$1». Проверено мутацией.
            свой = "org_id=$1" in q
            живой = "is_active=true" in q
            return sorted(
                [
                    i
                    for i in self.invite_links
                    if (not свой or i.get("org_id") == args[0])
                    and (not живой or i.get("is_active"))
                ],
                key=lambda i: str(i["created_at"]),
                reverse=True,
            )
        if q.startswith("SELECT * FROM users WHERE is_active = true AND org_id=$1"):
            # S-28: GET /api/users/ — до 07.08.2026 ветки не было вовсе,
            # то есть ручка не выполнялась в тестах ни разу. Отдаём СЫРУЮ
            # строку (с email и ролью), урезание — забота роутера: иначе
            # тест на урезание проверял бы FakePool, а не продакшен-код.
            return sorted(
                [
                    u
                    for u in self.users
                    if u.get("org_id") == args[0] and u.get("is_active", True)
                ],
                key=lambda u: u["id"],
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
        # REP-CRUD ЧП3: где уже лежат эти чеки (свой отчёт / чужой → 409).
        if q.startswith(
            "SELECT report_id, receipt_id FROM report_items WHERE receipt_id = ANY($1"
        ):
            ids = args[0]
            return [
                {"report_id": ri["report_id"], "receipt_id": ri["receipt_id"]}
                for ri in self.report_items
                if ri["receipt_id"] in ids
            ]
        if q.startswith("SELECT id, photo_key FROM receipts WHERE id = ANY($1"):
            # S-48: какие из удаляемых чеков несут снимок в хранилище.
            # A-ACL зеркалим целиком: org_id всегда, user_id — когда он есть
            # в запросе (не-admin). Пропустить эти условия здесь значит дать
            # тесту зелёный на НЕработающей изоляции — ровно случай T6.
            ids = args[0]
            org_id = args[1] if len(args) > 1 else None
            user_id = args[2] if len(args) > 2 else None
            return [
                {"id": r["id"], "photo_key": r.get("photo_key")}
                for r in self.receipts
                if r["id"] in ids
                and (org_id is None or r.get("org_id") == org_id)
                and (user_id is None or r.get("user_id") == user_id)
            ]
        if q.startswith("SELECT id, user_id FROM receipts WHERE id = ANY($1"):
            # S-15 IDOR-проверка создания отчёта: какие из запрошенных id реально
            # принадлежат орг пользователя (чужие/несуществующие сюда не попадут).
            # user_id — для инварианта REP-AUTHOR ЧП3 (все чеки одного автора).
            ids, org_id = args
            return [
                {"id": r["id"], "user_id": r.get("user_id")}
                for r in self.receipts
                if r["id"] in ids and r.get("org_id") == org_id
            ]
        # REP-CRUD ЧП2: развёрнутые чеки в деталях отчёта. SELECT * — та же
        # форма, что у списка чеков; A-ACL user_id=$3 для employee.
        if "WHERE receipts.id = ANY($1" in q:
            ids, org_id = args[0], args[1]
            user_id = args[2] if len(args) > 2 else None
            return sorted(
                [
                    _with_in_report(r, self)
                    for r in self.receipts
                    if r["id"] in ids
                    and r.get("org_id") == org_id
                    and (user_id is None or r.get("user_id") == user_id)
                ],
                key=lambda r: str(r["date"]),
                reverse=True,
            )
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
            # T35: org-scope БЕРЁТСЯ ИЗ ТЕКСТА. Раньше ветка считала подсказку
            # по чекам ВСЕХ организаций — то есть была позволительнее продакшена
            # (там `AND org_id=$2` из токена), и тест «подсказка не приходит
            # из чужой орг» не мог ни пройти честно, ни покраснеть.
            свой = "AND org_id=$2" in q
            counts = {}
            for r in self.receipts:
                if (
                    r["org"] == args[0]
                    and (not свой or r.get("org_id") == args[1])
                    and r["payment"]
                    and r["payment"] != "Не указано"
                ):
                    counts[r["payment"]] = counts.get(r["payment"], 0) + 1
            return {"payment": max(counts, key=counts.get)} if counts else None
        # REP-ACL: отчёт с author-scope (для не-can_see_all). ВАЖНО: проверка
        # стоит ДО org-scope-варианта ниже — тот более общий по префиксу
        # и иначе перехватил бы этот запрос, «потеряв» фильтр по автору.
        if q.startswith(
            "SELECT * FROM reports WHERE id=$1 AND org_id=$2 AND user_id=$3"
        ):
            return next(
                (
                    dict(r)
                    for r in self.reports
                    if r["id"] == args[0]
                    and r.get("org_id") == args[1]
                    and r.get("user_id") == args[2]
                ),
                None,
            )
        # REP-CRUD ЧП2: детали отчёта / гейт удаления (org-scope в самом SQL).
        if q.startswith("SELECT * FROM reports WHERE id=$1 AND org_id=$2"):
            return next(
                (
                    dict(r)
                    for r in self.reports
                    if r["id"] == args[0] and r.get("org_id") == args[1]
                ),
                None,
            )
        if q.startswith(
            "UPDATE reports SET status=$1 WHERE id=$2 AND org_id=$3 AND user_id=$4"
        ):
            for r in self.reports:
                if (
                    r["id"] == args[1]
                    and r.get("org_id") == args[2]
                    and r.get("user_id") == args[3]
                ):
                    r["status"] = args[0]
                    return dict(r)
            return None
        if q.startswith("SELECT status FROM reports WHERE id=$1 AND org_id=$2"):
            return next(
                (
                    {"status": r["status"]}
                    for r in self.reports
                    if r["id"] == args[0] and r.get("org_id") == args[1]
                ),
                None,
            )
        if "WHERE receipts.id=$1 AND receipts.org_id=$2" in q:
            # A-ACL: enforce org_id (и user_id для employee), а не только id.
            rid = args[0]
            org_id = args[1] if len(args) > 1 else None
            user_id = args[2] if len(args) > 2 else None
            return next(
                (
                    _with_in_report(r, self)
                    for r in self.receipts
                    if r["id"] == rid
                    and (org_id is None or r.get("org_id") == org_id)
                    and (user_id is None or r.get("user_id") == user_id)
                ),
                None,
            )
        if q.startswith(
            "SELECT photo_key, photo_url, raw_data FROM receipts WHERE id=$1"
        ):
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
                dict(
                    photo_key=r.get("photo_key"),
                    photo_url=r.get("photo_url"),
                    raw_data=r.get("raw_data"),
                )
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
            # 32-arg insert (org_id, date, … , category_id, user_id, vat_breakdown).
            # Зеркалит порядок колонок в receipts.py. card_id не вставляется → None.
            # NDS-CLEANUP ②: колонок стало 31 (ушли vat_20/vat_10, пришёл
            # vat_total), поэтому и добивка, и позиция fd_num сдвинулись.
            # Забыть здесь — значит уронить проверку дублей по причине,
            # не имеющей к дублям отношения: тест 409 падал на 200.
            args = list(args) + [None] * (31 - len(args))
            kkt_fn_val = args[6]
            fd_num_val = args[25]
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
                # NDS-CLEANUP ②: vat_20/vat_10 из INSERT убраны, на их месте
                # vat_0 и vat_total. Позиции сдвинулись — зеркало правится
                # вместе с продакшеном, иначе тесты падают не там, где причина
                # (сегодня так и вышло: 33 красных теста в шести файлах).
                vat_0=args[21],
                vat_total=args[22],
                kkt_serial=args[23],
                kkt_rn=args[24],
                fd_num=args[25],
                fpd=args[26],
                cashier=args[27],
                category_id=args[28],
                user_id=args[29],
                vat_breakdown=args[30],
                # photo_key добавлен ПОСЛЕДНИМ параметром намеренно: так позиции
                # остальных не сдвинулись, и зеркало не пришлось перенумеровывать
                # целиком (на этом уже горели — см. комментарий про NDS-CLEANUP выше).
                photo_key=args[31],
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
                status="Черновик",
                total=0,  # проставит _recalc_total, как в проде
                org_id=args[1] if len(args) > 1 else None,
                user_id=args[2] if len(args) > 2 else None,  # REP-AUTHOR
                created=date.today(),
                created_at=datetime.utcnow(),
            )
            self.reports.append(row)
            return dict(row)
        # Зеркало _recalc_total: сумма чеков состава, org-scope как в SQL.
        if q.startswith("UPDATE reports SET total"):
            for r in self.reports:
                if r["id"] == args[0] and r.get("org_id") == args[1]:
                    ids = [
                        i["receipt_id"]
                        for i in self.report_items
                        if i["report_id"] == r["id"]
                    ]
                    r["total"] = sum(
                        float(rc["amount"])
                        for rc in self.receipts
                        if rc["id"] in ids and rc.get("org_id") == args[1]
                    )
                    return dict(r)
            return None
        if q.startswith("UPDATE reports SET status=$1"):
            # T35: org-scope из текста — раньше статус менялся у отчёта
            # ЛЮБОЙ организации по одному id. Ветка с author-scope
            # («AND user_id=$4») стоит ВЫШЕ, эта — общая для org-варианта.
            свой = "AND org_id=$3" in q
            for r in self.reports:
                if r["id"] == args[1] and (not свой or r.get("org_id") == args[2]):
                    r["status"] = args[0]
                    return dict(r)
            return None
        if q.startswith("SELECT id FROM cards WHERE id=$1 AND org_id=$2"):
            # PATCH /api/cards/{id}/default сначала убеждается, что карта своя.
            return next(
                (
                    {"id": c["id"]}
                    for c in self.cards
                    if c["id"] == args[0] and c.get("org_id") == args[1]
                ),
                None,
            )
        # ── S-31: приглашения ───────────────────────────────────────────────
        if q.startswith("INSERT INTO invite_links (token, org_id, role"):
            self._invid += 1
            row = dict(
                id=self._invid,
                token=args[0],
                org_id=args[1],
                role=args[2],
                created_by=args[3],
                expires_at=args[4],
                max_uses=args[5],
                uses_count=0,
                is_active=True,
                created_at=datetime.now(timezone.utc),
            )
            self.invite_links.append(row)
            return dict(row)
        if q.startswith("SELECT * FROM invite_links WHERE token=$1"):
            return next(
                (dict(i) for i in self.invite_links if i["token"] == args[0]), None
            )
        # ── S-31: вход и сессии ─────────────────────────────────────────────
        if q.startswith("SELECT 1 FROM revoked_tokens WHERE token_hash=$1"):
            return (
                {"?column?": 1}
                if any(t["token_hash"] == args[0] for t in self.revoked_tokens)
                else None
            )
        if q.startswith("SELECT 1 FROM users WHERE id=$1 AND is_active=true"):
            return (
                {"?column?": 1}
                if any(
                    u["id"] == args[0] and u.get("is_active", True) for u in self.users
                )
                else None
            )
        if q.startswith("SELECT id FROM users WHERE lower(email)=$1"):
            # register-by-invite: занят ли email. Регистр сравнивается так же,
            # как в PostgreSQL — роутер уже привёл ввод к нижнему.
            return next(
                (
                    {"id": u["id"]}
                    for u in self.users
                    if (u.get("email") or "").lower() == args[0]
                ),
                None,
            )
        if q.startswith("SELECT * FROM users WHERE lower(email)=$1"):
            # S-31: регистрация смотрит, занят ли email. Отличается от ветки
            # register-by-invite («SELECT id …») тем, что нужна ВСЯ строка:
            # аккаунт без пароля можно «подхватить», а не отвергнуть.
            return next(
                (
                    dict(u)
                    for u in self.users
                    if (u.get("email") or "").lower() == args[0]
                ),
                None,
            )
        if q.startswith("INSERT INTO organizations (name, inn, type)"):
            # S-31: регистрация создаёт организацию. owner_id проставляется
            # отдельным UPDATE ниже — он в той же транзакции.
            self._orgid = max([o["id"] for o in self.organizations] + [0]) + 1
            self.organizations.append(
                dict(
                    id=self._orgid,
                    name=args[0],
                    inn=args[1],
                    type=args[2],
                    owner_id=None,
                    created_at=datetime.now(timezone.utc),
                    tax_system=None,
                )
            )
            return {"id": self._orgid}
        if q.startswith("UPDATE users SET password_hash=$1, phone=COALESCE($2, phone)"):
            # S-31: «подхват» засеянного аккаунта без пароля — организация
            # и данные остаются его, новая НЕ создаётся. Ветка стоит ВЫШЕ
            # общей «UPDATE users SET … RETURNING *»: та разбирает колонки
            # позиционно и ждёт (id, org_id) в хвосте, а здесь хвост — один id.
            for u in self.users:
                if u["id"] == args[6]:
                    u["password_hash"] = args[0]
                    u["phone"] = args[1] if args[1] is not None else u.get("phone")
                    u["first_name"] = args[2] or u.get("first_name")
                    u["last_name"] = args[3] or u.get("last_name")
                    u["is_email_verified"] = args[4]
                    u["email_verify_token"] = args[5]
                    return dict(u)
            return None
        if q.startswith("SELECT * FROM users WHERE email_verify_token=$1"):
            # S-31: подтверждение почты по ссылке из письма.
            return next(
                (dict(u) for u in self.users if u.get("email_verify_token") == args[0]),
                None,
            )
        if q.startswith("SELECT * FROM users WHERE lower(email)=lower($1) OR phone=$1"):
            # login: вход по email ИЛИ телефону одним параметром.
            ident = args[0]
            return next(
                (
                    dict(u)
                    for u in self.users
                    if (u.get("email") or "").lower() == ident.lower()
                    or (u.get("phone") and u["phone"] == ident)
                ),
                None,
            )
        if "VALUES ($1,$2,$3,$4,$5,'admin'" in q:
            # S-31: РЕГИСТРАЦИЯ. Роль здесь ЛИТЕРАЛ 'admin' в SQL, поэтому
            # аргументов восемь, а не девять, как у регистрации по ссылке.
            # Ветки различаются именно этим: общий префикс колонок у них
            # одинаковый, и одна молча разбирала бы аргументы другой
            # (поймано первым же прогоном — IndexError).
            self._uid += 1
            row = dict(
                id=self._uid,
                first_name=args[0],
                last_name=args[1],
                email=args[2],
                phone=args[3],
                password_hash=args[4],
                role="admin",
                org_id=args[5],
                is_email_verified=args[6],
                email_verify_token=args[7],
                patronymic=None,
                is_active=True,
            )
            self.users.append(row)
            return dict(row)
        if q.startswith("INSERT INTO users (first_name,last_name,email,phone"):
            # register-by-invite: роль и орг берутся ИЗ ПРИГЛАШЕНИЯ, не из тела —
            # это и проверяют тесты. Порядок колонок отличается от POST /api/users/
            # (там patronymic и нет пароля), поэтому ветки различаются началом
            # списка колонок, а не общим «INSERT INTO users» — иначе одна
            # перехватила бы другую (сторож T6).
            self._uid += 1
            row = dict(
                id=self._uid,
                first_name=args[0],
                last_name=args[1],
                email=args[2],
                phone=args[3],
                password_hash=args[4],
                role=args[5],
                org_id=args[6],
                is_email_verified=args[7],
                email_verify_token=args[8],
                patronymic=None,
                is_active=True,
            )
            self.users.append(row)
            return dict(row)
        if q.startswith("INSERT INTO users (first_name, last_name, patronymic"):
            # S-29: POST /api/users/ — только admin, но FakePool про роли не знает,
            # его дело — повторить SQL. Гейт проверяется тестами через 403.
            self._uid += 1
            row = dict(
                id=self._uid,
                first_name=args[0],
                last_name=args[1],
                patronymic=args[2],
                email=args[3],
                role=args[4],
                org_id=args[5],
                is_active=True,
                password_hash=None,
            )
            self.users.append(row)
            return dict(row)
        if q.startswith("UPDATE users SET") and "RETURNING *" in q:
            # Набор колонок динамический (UPDATABLE), значения идут по порядку,
            # последними — id и org_id.
            cols = [
                # T38: имя колонки берём до «=» БЕЗ оглядки на пробелы вокруг.
                # Раньше стояло split(" = ") — двойник умел читать только один
                # стиль записи, и переход роутеров на общий сборщик (`col=$1`)
                # молча перестал что-либо обновлять: тесты падали не там, где
                # была причина.
                c.split("=")[0].strip()
                for c in q.split("SET ", 1)[1].split(" WHERE ")[0].split(",")
            ]
            uid, org = args[-2], args[-1]
            for u in self.users:
                if u["id"] == uid and u.get("org_id") == org:
                    for i, c in enumerate(cols):
                        u[c] = args[i]
                    return dict(u)
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
            # T35: org-scope из текста — раньше переименовывалась карта
            # ЛЮБОЙ организации по одному id.
            свой = "AND org_id=$3" in q
            for c in self.cards:
                if c["id"] == args[1] and (not свой or c.get("org_id") == args[2]):
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
        if q.startswith(
            "SELECT consent_at, policy_version, consent_text FROM user_consents"
        ):
            # S-31: профиль (_me_payload) ищет согласие по user_id ИЛИ email,
            # плюс легаси 'local_user' — три значения в IN.
            # S-34: отдаём и ЗАМОРОЖЕННЫЙ ТЕКСТ записи — Настройки показывают
            # то, на что человек соглашался, а не текущую редакцию.
            ключи = {str(args[0]), str(args[1]), "local_user"}
            matches = [c for c in self.consents if str(c["user_id"]) in ключи]
            if not matches:
                return None
            latest = max(matches, key=lambda c: c["consent_at"])
            return {
                "consent_at": latest["consent_at"],
                "policy_version": latest.get("policy_version"),
                "consent_text": latest.get("consent_text"),
            }
        if q.startswith("SELECT * FROM users WHERE id=$1"):
            # Обслуживает ДВА запроса: `PATCH /me` перечитывает свою строку,
            # а get_current_user (app/auth.py) добавляет `AND is_active=true`.
            # T35: флаг БЕРЁТСЯ ИЗ ТЕКСТА. Пока он был зашит «всегда отдавать»,
            # отключённый пользователь в тестах продолжал ходить с токеном —
            # то есть ЛЮБАЯ проверка деактивации (включая S-29) была слепа.
            живой = "AND is_active=true" in q
            return next(
                (
                    dict(u)
                    for u in self.users
                    if u["id"] == args[0] and (not живой or u.get("is_active", True))
                ),
                None,
            )
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
        # ЗДЕСЬ БЫЛА МЁРТВАЯ ВЕТКА «DELETE FROM receipts WHERE date=$1»:
        # она распаковывала ЧЕТЫРЕ аргумента, а роутер давно шлёт ПЯТЬ
        # (в запрос добавили org_id) — то есть при первом же выполнении она
        # упала бы с ValueError. Не падала ровно потому, что ручка чистки
        # дублей ни разу не выполнялась в тестах: замер покрытия показывал
        # у неё «только 403». Рабочая ветка — ниже, рядом с остальными
        # запросами чистки, и org-scope в ней берётся из текста запроса.
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
            # Зеркало uq_report_items_receipt_id: чек живёт ровно в одном отчёте
            # (один чек в двух авансовых отчётах = двойное возмещение).
            if any(ri["receipt_id"] == args[1] for ri in self.report_items):
                raise asyncpg.exceptions.UniqueViolationError(
                    "duplicate key value violates unique constraint "
                    '"uq_report_items_receipt_id"'
                )
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
        # ── S-31: расход и отзыв приглашения ────────────────────────────────
        if q.startswith("UPDATE invite_links SET uses_count=$1, is_active=$2"):
            for i in self.invite_links:
                if i["id"] == args[2]:
                    i["uses_count"] = args[0]
                    i["is_active"] = args[1]
            return "UPDATE 1"
        if q.startswith("UPDATE invite_links SET is_active=false WHERE token=$1"):
            # org-scope БЕРЁТСЯ ИЗ ТЕКСТА, а не зашит (см. такую же ветку
            # в fetch): ручка отвечает ok всегда, поэтому единственный признак
            # работающего scope — состояние чужого приглашения. Зашей фильтр
            # здесь — и тест перестал бы уметь краснеть.
            свой = "AND org_id=$2" in q
            for i in self.invite_links:
                if i["token"] == args[0] and (not свой or i.get("org_id") == args[1]):
                    i["is_active"] = False
            return "UPDATE 1"
        if q.startswith("DELETE FROM receipts WHERE date=$1 AND amount=$2 AND org=$3"):
            # S-31: чистка дублей — оставляем keep_id. org-scope из текста.
            свой = "AND org_id=$4" in q
            self.receipts = [
                r
                for r in self.receipts
                if not (
                    r["date"] == args[0]
                    and r["amount"] == args[1]
                    and r["org"] == args[2]
                    and (not свой or r.get("org_id") == args[3])
                    and r["id"] != args[4]
                )
            ]
            return "DELETE"
        if q.startswith("UPDATE organizations SET owner_id=$1 WHERE id=$2"):
            # S-31: владелец новой орг — тот, кто её зарегистрировал.
            for o in self.organizations:
                if o["id"] == args[1]:
                    o["owner_id"] = args[0]
            return "UPDATE 1"
        if q.startswith("INSERT INTO revoked_tokens (token_hash, expires_at)"):
            self.revoked_tokens.append(dict(token_hash=args[0], expires_at=args[1]))
            return "INSERT"
        # S-16: чистка просроченных отзывов. Двойник УДАЛЯЕТ ПО-НАСТОЯЩЕМУ,
        # а не отвечает «ок»: иначе тест «после чистки список короче» прошёл бы
        # и при неработающем DELETE — ровно тот класс, из-за которого заведён
        # T35 (двойник позволительнее продакшена).
        if q.startswith("DELETE FROM revoked_tokens WHERE expires_at <"):
            было = len(self.revoked_tokens)
            сейчас = datetime.now(timezone.utc)

            def _жив(t):
                срок = t.get("expires_at")
                if срок is None:
                    return True
                # Наивную дату приводим к UTC: настоящий PostgreSQL хранит
                # TIMESTAMPTZ и сравнивает всегда, а падение двойника
                # по TypeError было бы поломкой теста, а не находкой.
                if срок.tzinfo is None:
                    срок = срок.replace(tzinfo=timezone.utc)
                return срок >= сейчас

            self.revoked_tokens = [t for t in self.revoked_tokens if _жив(t)]
            return f"DELETE {было - len(self.revoked_tokens)}"
        # S-16: смена пароля ставит отметку отзыва тем же запросом.
        if q.startswith("UPDATE users SET password_hash=$1, tokens_valid_from=$2"):
            for u in self.users:
                if u["id"] == args[2]:
                    u["password_hash"] = args[0]
                    u["tokens_valid_from"] = args[1]
            return "UPDATE 1"
        # S-16: «выйти на всех устройствах».
        if q.startswith("UPDATE users SET tokens_valid_from=$1 WHERE id=$2"):
            for u in self.users:
                if u["id"] == args[1]:
                    u["tokens_valid_from"] = args[0]
            return "UPDATE 1"
        # ── S-31: счётчик неудачных попыток входа ───────────────────────────
        if q.startswith("UPDATE users SET failed_attempts=$1, locked_until=$2"):
            for u in self.users:
                if u["id"] == args[2]:
                    u["failed_attempts"] = args[0]
                    u["locked_until"] = args[1]
            return "UPDATE 1"
        if q.startswith("UPDATE users SET failed_attempts=0, locked_until=NULL"):
            for u in self.users:
                if u["id"] == args[0]:
                    u["failed_attempts"] = 0
                    u["locked_until"] = None
                    u["last_login_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"
        if q.startswith(
            "UPDATE users SET is_email_verified=true, email_verify_token=NULL"
        ):
            for u in self.users:
                if u["id"] == args[0]:
                    u["is_email_verified"] = True
                    u["email_verify_token"] = None
            return "UPDATE 1"
        if q.startswith("UPDATE users SET password_hash=$1 WHERE id=$2"):
            for u in self.users:
                if u["id"] == args[1]:
                    u["password_hash"] = args[0]
            return "UPDATE 1"
        if q.startswith("UPDATE users SET is_active = false"):
            for u in self.users:
                if u["id"] == args[0] and u.get("org_id") == args[1]:
                    u["is_active"] = False
            return "UPDATE 1"
        # ВНИЗУ НАМЕРЕННО: шаблон общий («UPDATE users SET»), и выше него
        # стоят все специфичные ветки users. Поставил её сначала выше —
        # сторож T6 показал перехват строкой и строкой, за что и заведён.
        if q.startswith("UPDATE users SET") and q.endswith(
            "WHERE id = $" + str(len(args))
        ):
            # S-31: PATCH /me — набор колонок динамический, но СОБИРАЕТСЯ
            # ИЗ БЕЛОГО СПИСКА в роутере. Ветка повторяет SQL как есть: если
            # белый список расширят, сюда прилетит лишняя колонка и тест
            # «роль через /me не меняется» покраснеет.
            cols = [
                # T38: имя колонки берём до «=» БЕЗ оглядки на пробелы вокруг.
                # Раньше стояло split(" = ") — двойник умел читать только один
                # стиль записи, и переход роутеров на общий сборщик (`col=$1`)
                # молча перестал что-либо обновлять: тесты падали не там, где
                # была причина.
                c.split("=")[0].strip()
                for c in q.split("SET ", 1)[1].split(" WHERE ")[0].split(",")
            ]
            for u in self.users:
                if u["id"] == args[-1]:
                    for i, c in enumerate(cols):
                        u[c] = args[i]
            return "UPDATE 1"
        if q.startswith("UPDATE cards SET is_default = (id = $1)"):
            for c in self.cards:
                if c.get("org_id") == args[1]:
                    c["is_default"] = c["id"] == args[0]
            return "UPDATE 1"
        if q.startswith("DELETE FROM cards WHERE id=$1"):
            # org-scope БЕРЁТСЯ ИЗ ТЕКСТА (см. правило в шапке класса): раньше
            # ветка удаляла карту по одному id, игнорируя org_id, — то есть
            # была ПОЗВОЛИТЕЛЬНЕЕ продакшена. Тест «чужую карту не удалили»
            # с таким двойником не мог бы пройти, а снятие org-scope
            # в роутере не заметил бы никто.
            свой = "AND org_id=$2" in q
            self.cards = [
                c
                for c in self.cards
                if not (c["id"] == args[0] and (not свой or c.get("org_id") == args[1]))
            ]
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
        # REP-CRUD ЧП2: удаление отчёта. Состав уходит каскадом — зеркалим
        # ON DELETE CASCADE на report_id, чеки при этом не трогаем.
        if q.startswith("DELETE FROM reports WHERE id=$1 AND org_id=$2"):
            before = len(self.reports)
            self.reports = [
                r
                for r in self.reports
                if not (r["id"] == args[0] and r.get("org_id") == args[1])
            ]
            if before != len(self.reports):
                self.report_items = [
                    ri for ri in self.report_items if ri["report_id"] != args[0]
                ]
            return f"DELETE {before - len(self.reports)}"
        # REP-CRUD ЧП3: убрать один чек из отчёта (org-scope по чеку).
        if q.startswith(
            "DELETE FROM report_items WHERE report_id=$1 AND receipt_id=$2"
        ):
            report_id, receipt_id, org_id = args
            own = {r["id"] for r in self.receipts if r.get("org_id") == org_id}
            before = len(self.report_items)
            self.report_items = [
                ri
                for ri in self.report_items
                if not (
                    ri["report_id"] == report_id
                    and ri["receipt_id"] == receipt_id
                    and receipt_id in own
                )
            ]
            return f"DELETE {before - len(self.report_items)}"
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
        "users",
        "invite_links",
        "revoked_tokens",
        "receipts",
        "receipt_items",
        "reports",
        "report_items",
        "cards",
        "consents",
        "category_groups",
        "categories",
    )
    _COUNTERS = (
        "_rid",
        "_repid",
        "_cid",
        "_consid",
        "_gid",
        "_catid",
        "_uid",
        "_invid",
    )

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
    db.users.append(
        dict(
            id=2,
            first_name="Иван",
            last_name="Петров",
            patronymic=None,
            email="ivan@example.com",
            role="employee",
            org_id=1,
            is_active=True,
            password_hash=None,
        )
    )
    db._uid = 2
    db.cards.append(dict(id=1, name="Корп.карта", org_id=1, created_at=now))
    db._cid = 1
    db.reports.append(
        dict(
            id=1,
            title="Отчёт за май",
            status="Черновик",
            total=5000.0,
            org_id=1,
            created=date(2026, 5, 10),
            created_at=now,
        )
    )
    db._repid = 1
    return db


def _override_user(role, user_id=1, org_id=1):
    """Подменяет get_current_user фиксированным юзером с заданной ролью.

    org_id параметром (S-31): проверка org-scope требует ВТОРОЙ организации —
    «админ чужой орг не гасит наше приглашение» иначе не выражается.
    """
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
    """Сменить роль смотрящего внутри уже поднятого клиента (S-28).

    Нужна там, где проверяется РЕАКЦИЯ НА РОЛЬ, а не отдельный сценарий:
    заводить по клиенту на каждую роль (включая будущую manager из Финансов)
    дороже, чем подменить зависимость на месте. Клиентские фикстуры чистят
    dependency_overrides на выходе, так что подмена не течёт в соседний тест.
    """
    return _override_user


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
