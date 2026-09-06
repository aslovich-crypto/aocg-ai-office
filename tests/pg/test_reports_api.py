# -*- coding: utf-8 -*-
"""Ручки отчётов — НА ЖИВОЙ БАЗЕ (T36, перевод test_api.py, заход 2).

ПЕРЕВЕДЕНО С FakePool 06.09.2026. Тесты те же, прибор другой: создание и
смена статуса, контракт форм ответа, удаление по статусам, автор отчёта,
кто утверждает, видимость по ролям, состав отчёта, производный `total`
и скоуп `receiptIds` по организации.

⚠️ ПОЧЕМУ ОТЧЁТЫ ЗДЕСЬ ВАЖНЕЕ ВСЕГО. `total` отчёта — величина ПРОИЗВОДНАЯ:
она обязана равняться сумме чеков состава. Двойник считал эту сумму питоном,
своим циклом по списку, а не запросом к базе; 04.09 на проде нашлась пара
«отчёт с суммой 1.00 и пустым составом» (T166). Зеркало, которое считает само,
такое скрывает по построению — поэтому здесь сумма проверяется ЗАПРОСОМ
`сумма_состава`, идущим к настоящей базе мимо ответа ручки.

⚠️ АВТОРЫ ОБЯЗАНЫ СУЩЕСТВОВАТЬ: `reports.user_id` — NOT NULL и настоящий FK,
`receipts.user_id` — тоже FK. Отсюда автоиспользуемая фикстура `люди`: админ
id=1 (им ходят `client` и `client_accountant`) и сотрудник id=2 (`client_employee`).
"""

from datetime import date, datetime

import pytest
import pytest_asyncio

ADMIN_ID = 1  # client и client_accountant — user_id=1
EMP_ID = 2  # client_employee — user_id=2
ORG = 1


@pytest_asyncio.fixture(autouse=True)
async def люди(db):
    """Смотрящие, которыми ходят фикстуры клиентов. Автоиспользуемая: почти
    каждый тест здесь заводит отчёт или чек, а у обоих автор — настоящий FK."""
    await db.обеспечить_пользователя(id=ADMIN_ID, first_name="Админ", role="admin")
    await db.обеспечить_пользователя(
        id=EMP_ID, first_name="Иван", last_name="Петров", role="employee"
    )
    return db


async def _сумма_сходится(db, report_id):
    """`total` отчёта равен сумме чеков состава — ЗАПРОСОМ К БАЗЕ, мимо ответа.

    ⚠️ РАДИ ЭТОГО ЗАХОД И ЗАТЕВАЛСЯ. `total` — величина ПРОИЗВОДНАЯ, и держится
    она на том, что её пересчитали. Двойник складывал сумму питоном, своим
    циклом по списку словарей: тест спрашивал ручку, ручка спрашивала зеркало,
    зеркало отвечало тем, что само же и сложило — круг замкнут, и разойтись
    внутри него нечему. 04.09.2026 на проде нашлась пара «отчёт id 10, `total`
    1.00, состав ПУСТОЙ» (T166) — ровно то, чего такая проверка увидеть не
    может. Здесь сумма берётся ВТОРЫМ, независимым запросом к настоящей базе.
    """
    хранится = (await db.отчёт(report_id))["total"]
    по_составу = await db.сумма_состава(report_id)
    assert float(хранится) == float(по_составу), (
        f"отчёт {report_id}: в строке total={хранится}, "
        f"а чеки состава дают {по_составу}"
    )


ЧУЖАЯ_ОРГ = 999
ЧУЖОЙ_ID = 999


async def _чужой_автор(db):
    """Автор отчёта чужой организации.

    ⚠️ У ДВОЙНИКА ОТЧЁТ ЖИЛ БЕЗ АВТОРА ВООБЩЕ. `reports.user_id` — NOT NULL и
    настоящий FK на `users` (REP-AUTHOR). Тесты «чужой отчёт неотличим от
    несуществующего» заводили строку вовсе без `user_id`: на живой базе такой
    отчёт не заводится, а зеркало его принимало — и проверка изоляции шла на
    состоянии, которого не бывает."""
    await db.обеспечить_пользователя(
        id=ЧУЖОЙ_ID, first_name="Чужой", role="admin", org_id=ЧУЖАЯ_ОРГ
    )
    return ЧУЖОЙ_ID


async def _mk(
    db, rid, *, source="manual", kkt_fn=None, org_id=ORG, amount=100.0, user_id=ADMIN_ID
):
    """Чек с заданным автором — сырьё для состава отчёта."""
    await db.добавить_чек(
        id=rid,
        org=f"Org{rid}",
        amount=amount,
        date=date(2026, 5, 20),
        payment=None,
        kkt_fn=kkt_fn,
        org_id=org_id,
        user_id=user_id,  # REP-AUTHOR: у чека всегда есть владелец
        source=source,
    )


# ─── GET /api/reports/ ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_reports_returns_list(client):
    resp = await client.get("/api/reports/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ─── POST /api/reports/ ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_report(client):
    rc = await client.post(
        "/api/receipts/", json={"date": "2026-05-14", "org": "Лента", "amount": 999.0}
    )
    rid = rc.json()["id"]

    resp = await client.post(
        "/api/reports/",
        json={"title": "Майский отчёт", "total": 999.0, "receiptIds": [rid]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] > 0
    assert body["title"] == "Майский отчёт"
    assert body["receiptIds"] == [rid]


# ─── PATCH /api/reports/{id} ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_patch_report_status(client, seeded):
    resp = await client.patch("/api/reports/1", json={"status": "На проверке"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "На проверке"


@pytest.mark.asyncio
async def test_patch_report_status_invalid_rejected(client, seeded):
    # Статус вне жизненного цикла (в т.ч. старый 'Личные') — 422, не проходит.
    resp = await client.patch("/api/reports/1", json={"status": "Личные"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_report_returns_receipt_ids(client):
    # Ответ PATCH — той же формы, что GET: с составом чеков. Без этого клиент,
    # подставляя ответ в список, показывал «0 чеков».
    rc = await client.post(
        "/api/receipts/", json={"date": "2026-07-01", "org": "Лента", "amount": 100.0}
    )
    rid = rc.json()["id"]
    created = await client.post(
        "/api/reports/",
        json={"title": "Июль", "total": 100.0, "receiptIds": [rid]},
    )
    report_id = created.json()["id"]

    resp = await client.patch(
        f"/api/reports/{report_id}", json={"status": "На проверке"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "На проверке"
    assert body["receiptIds"] == [rid]

    # Форма ответа PATCH совпадает с формой элемента списка GET.
    listed = await client.get("/api/reports/")
    item = next(r for r in listed.json() if r["id"] == report_id)
    assert set(body) == set(item)


# ─── REP-CRUD ЧП2: контракт форм ──────────────────────────────────────
@pytest.mark.asyncio
async def test_report_shapes_post_patch_list_identical(client):
    # Один ресурс — одна форма: POST == PATCH == элемент GET-списка.
    # Разъезд форм уже приводил к багу «0 чеков» после смены статуса.
    rc = await client.post(
        "/api/receipts/", json={"date": "2026-07-10", "org": "Форма", "amount": 42.0}
    )
    created = await client.post(
        "/api/reports/", json={"title": "Формы", "receiptIds": [rc.json()["id"]]}
    )
    assert created.status_code == 200
    rid = created.json()["id"]
    patched = await client.patch(f"/api/reports/{rid}", json={"status": "На проверке"})
    listed = await client.get("/api/reports/")
    item = next(r for r in listed.json() if r["id"] == rid)
    assert set(created.json()) == set(patched.json()) == set(item)


@pytest.mark.asyncio
async def test_get_report_detail_has_list_fields_plus_receipts(client):
    # GET /{id} — надмножество элемента списка: все его поля + receipts.
    rc = await client.post(
        "/api/receipts/", json={"date": "2026-07-11", "org": "Деталь", "amount": 15.0}
    )
    receipt_id = rc.json()["id"]
    created = await client.post(
        "/api/reports/", json={"title": "Детали", "receiptIds": [receipt_id]}
    )
    rid = created.json()["id"]

    detail = await client.get(f"/api/reports/{rid}")
    assert detail.status_code == 200
    body = detail.json()

    listed = await client.get("/api/reports/")
    item = next(r for r in listed.json() if r["id"] == rid)
    assert set(item).issubset(set(body))  # ничего не потеряли
    assert set(body) - set(item) == {"receipts"}  # добавили ровно развёрнутые чеки

    assert body["receiptIds"] == [receipt_id]
    assert len(body["receipts"]) == 1
    # Форма чека в деталях = форма чека в списке чеков (оба SELECT *).
    all_receipts = await client.get("/api/receipts/")
    listed_receipt = next(r for r in all_receipts.json() if r["id"] == receipt_id)
    assert set(body["receipts"][0]) == set(listed_receipt)


# ─── REP-CRUD ЧП2: GET /{id} ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_report_detail_not_found(client):
    resp = await client.get("/api/reports/99999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_report_detail_foreign_org_404(client, db):
    # Чужой отчёт неотличим от несуществующего.
    await db.добавить_отчёт(
        id=777,
        title="Чужой",
        user_id=await _чужой_автор(db),
        total=0,
        status="Черновик",
        org_id=999,
        created=date(2026, 7, 1),
    )
    resp = await client.get("/api/reports/777")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_report_detail_empty_report(client):
    created = await client.post(
        "/api/reports/", json={"title": "Пустой", "receiptIds": []}
    )
    resp = await client.get(f"/api/reports/{created.json()['id']}")
    assert resp.status_code == 200
    assert resp.json()["receiptIds"] == [] and resp.json()["receipts"] == []


# ─── REP-CRUD ЧП2: DELETE /{id} ───────────────────────────────────────
@pytest.mark.asyncio
async def test_delete_report_draft_ok(client, db):
    rc = await client.post(
        "/api/receipts/", json={"date": "2026-07-12", "org": "Удал", "amount": 7.0}
    )
    created = await client.post(
        "/api/reports/", json={"title": "Черновик", "receiptIds": [rc.json()["id"]]}
    )
    rid = created.json()["id"]

    resp = await client.delete(f"/api/reports/{rid}")
    assert resp.status_code == 204
    assert await db.отчёт(rid) is None
    # Состав ушёл каскадом, сам чек остался и снова свободен.
    assert all(с["report_id"] != rid for с in await db.связи_отчётов())
    assert await db.чек(rc.json()["id"]) is not None


@pytest.mark.asyncio
async def test_delete_report_rejected_ok(client, db):
    created = await client.post(
        "/api/reports/", json={"title": "Отклонённый", "receiptIds": []}
    )
    rid = created.json()["id"]
    await client.patch(f"/api/reports/{rid}", json={"status": "На проверке"})
    await client.patch(
        f"/api/reports/{rid}", json={"status": "Отклонён", "reason": "чек без НДС"}
    )

    resp = await client.delete(f"/api/reports/{rid}")
    assert resp.status_code == 204
    assert await db.отчёт(rid) is None


@pytest.mark.asyncio
async def test_delete_report_in_review_409_says_recall_first(client, db):
    created = await client.post(
        "/api/reports/", json={"title": "На проверке", "receiptIds": []}
    )
    rid = created.json()["id"]
    await client.patch(f"/api/reports/{rid}", json={"status": "На проверке"})

    resp = await client.delete(f"/api/reports/{rid}")
    assert resp.status_code == 409
    assert "отзовите" in resp.json()["detail"]  # текст объясняет следующий шаг
    assert await db.отчёт(rid) is not None  # отчёт на месте


@pytest.mark.asyncio
async def test_delete_report_approved_409(client, db):
    created = await client.post(
        "/api/reports/", json={"title": "Одобренный", "receiptIds": []}
    )
    rid = created.json()["id"]
    await client.patch(f"/api/reports/{rid}", json={"status": "На проверке"})
    await client.patch(f"/api/reports/{rid}", json={"status": "Одобрен"})

    resp = await client.delete(f"/api/reports/{rid}")
    assert resp.status_code == 409
    assert "принят к учёту" in resp.json()["detail"]
    assert await db.отчёт(rid) is not None


# ─── REP-AUTHOR ЧП1: автор отчёта ─────────────────────────────────────
@pytest.mark.asyncio
async def test_create_report_stores_author(client, db):
    # Автор отчёта = создатель (АО-1 — документ конкретного подотчётного лица).
    rc = await client.post(
        "/api/receipts/", json={"date": "2026-07-30", "org": "Автор", "amount": 12.0}
    )
    resp = await client.post(
        "/api/reports/", json={"title": "С автором", "receiptIds": [rc.json()["id"]]}
    )
    assert resp.status_code == 200
    # Фикстура client ходит фиксированным пользователем id=1 (см. _override_user).
    assert resp.json()["user_id"] == 1
    stored = await db.отчёт(resp.json()["id"])
    assert stored["user_id"] == 1


@pytest.mark.asyncio
async def test_report_author_in_all_shapes(client):
    # user_id виден во всех формах ответа (POST/PATCH/список/детали) —
    # они все SELECT *, поэтому колонка появляется везде разом.
    created = await client.post(
        "/api/reports/", json={"title": "Формы автора", "receiptIds": []}
    )
    rid = created.json()["id"]
    patched = await client.patch(f"/api/reports/{rid}", json={"status": "На проверке"})
    listed = await client.get("/api/reports/")
    item = next(r for r in listed.json() if r["id"] == rid)
    detail = await client.get(f"/api/reports/{rid}")
    for body in (created.json(), patched.json(), item, detail.json()):
        assert "user_id" in body


# ─── T7: МАТРИЦА КОНТРАКТА ФОРМ ОТВЕТА ────────────────────────────────
# Класс багов: мутирующая ручка отдаёт форму БЕДНЕЕ элемента списка, клиент
# подставляет ответ в список — и на экране пропадают поля. Дважды доходило
# до прода: PATCH отчёта терял receiptIds («0 чеков»), PATCH чека терял
# in_report (кнопка «Прикрепить» показывала неверное состояние).
#
# Эталон = элемент соответствующего GET-списка. Сравниваем МНОЖЕСТВА КЛЮЧЕЙ:
# отсутствие поля — всегда провал; лишние поля разрешены только те, что
# объявлены явно (extra) — так надмножества (детали отчёта = список +
# receipts) остаются законными, а случайный «довесок» ловится.
#
# ЗАЧЕМ МАТРИЦА, а не тест на ручку: точечный тест ловит свой случай и молчит
# про соседний. Новая ручка = одна строка ниже, а не новый тест.
#
# СОЗНАТЕЛЬНО ВНЕ МАТРИЦЫ (не забыты — не отдают объект ресурса):
#   DELETE /api/reports/{id}          → 204 без тела;
#   DELETE /api/receipts/{id}         → {"ok": true}, статус операции;
#   POST   /api/receipts/bulk-delete  → сводка (deleted/blocked_*), не чек;
#   GET    /api/receipts/{id}/photo   → бинарь/редирект;
#   GET    /api/receipts/suggest-payment, POST /api/receipts/ocr/ → не ресурс.
# Ограничение: прогон идёт на FakePool — он зеркалит запросы вручную,
# поэтому полностью класс закроет только настоящий PostgreSQL в CI (FIN-ТД).
def _check_shape(name, resp, reference, extra=frozenset()):
    assert resp.status_code in (200, 201), f"{name}: HTTP {resp.status_code}"
    keys = set(resp.json())
    missing = reference - keys
    unexpected = keys - reference - set(extra)
    assert not missing, f"{name}: НЕ ХВАТАЕТ полей {sorted(missing)}"
    assert not unexpected, f"{name}: ЛИШНИЕ поля {sorted(unexpected)}"


@pytest.mark.asyncio
async def test_shape_contract_receipts(client, db):
    """T7: каждая ручка чека отдаёт форму элемента GET /api/receipts/.

    Чек создаём через API, а не хелпером _mk: _mk кладёт в фейковое хранилище
    УРЕЗАННУЮ строку (несколько полей), тогда как в проде `SELECT *` всегда
    отдаёт все колонки. Эталон из _mk был бы беднее реального и прятал бы
    расхождения — берём его из полноценной строки.
    """
    made = await client.post(
        "/api/receipts/",
        json={"date": "2026-08-01", "org": "Матрица", "amount": 5.0},
    )
    rid = made.json()["id"]
    await client.post("/api/reports/", json={"title": "Матрица", "receiptIds": [rid]})

    listed = await client.get("/api/receipts/")
    reference = set(next(r for r in listed.json() if r["id"] == rid))

    _check_shape(
        "GET /api/receipts/{id}",
        await client.get(f"/api/receipts/{rid}"),
        reference,
    )
    _check_shape(
        "PATCH /api/receipts/{id} (с полями)",
        await client.patch(f"/api/receipts/{rid}", json={"payment": "Наличные"}),
        reference,
    )
    _check_shape(
        "PATCH /api/receipts/{id} (без полей)",
        await client.patch(f"/api/receipts/{rid}", json={}),
        reference,
    )
    _check_shape(
        "POST /api/receipts/",
        await client.post(
            "/api/receipts/",
            json={"date": "2026-08-02", "org": "Другая", "amount": 7.0},
        ),
        reference,
        # warning — предупреждение о возможном дубле (задача №9), не поле чека.
        extra={"warning"},
    )


@pytest.mark.asyncio
async def test_shape_contract_reports(client, db):
    """T7: каждая ручка отчёта отдаёт форму элемента GET /api/reports/."""
    await _mk(db, 810, user_id=1)
    await _mk(db, 811, user_id=1)
    created = await client.post(
        "/api/reports/", json={"title": "Матрица-отчёт", "receiptIds": [810]}
    )
    rid = created.json()["id"]
    listed = await client.get("/api/reports/")
    reference = set(next(r for r in listed.json() if r["id"] == rid))

    _check_shape("POST /api/reports/", created, reference)
    _check_shape(
        "POST /api/reports/{id}/receipts",
        await client.post(f"/api/reports/{rid}/receipts", json={"receiptIds": [811]}),
        reference,
    )
    _check_shape(
        "DELETE /api/reports/{id}/receipts/{rid}",
        await client.delete(f"/api/reports/{rid}/receipts/811"),
        reference,
    )
    _check_shape(
        "PATCH /api/reports/{id}",
        await client.patch(f"/api/reports/{rid}", json={"status": "На проверке"}),
        reference,
    )
    _check_shape(
        "GET /api/reports/{id}",
        await client.get(f"/api/reports/{rid}"),
        reference,
        # детали — законное надмножество: тот же отчёт + развёрнутые чеки.
        extra={"receipts"},
    )


# ─── ЧП4а: флаг in_report у чека ──────────────────────────────────────
@pytest.mark.asyncio
async def test_receipts_list_has_in_report_flag(client, db):
    # Кнопке «Прикрепить к отчёту» нужно знать, свободен ли чек.
    await _mk(db, 700, user_id=1)  # свободный
    await _mk(db, 701, user_id=1)  # уйдёт в отчёт
    await client.post("/api/reports/", json={"title": "С чеком", "receiptIds": [701]})

    resp = await client.get("/api/receipts/")
    assert resp.status_code == 200
    by_id = {r["id"]: r for r in resp.json()}
    assert by_id[701]["in_report"] is True
    assert by_id[700]["in_report"] is False


@pytest.mark.asyncio
async def test_receipt_detail_has_in_report_flag(client, db):
    # Карточку открывают и напрямую — форма чека не должна зависеть от пути.
    await _mk(db, 702, user_id=1)
    free = await client.get("/api/receipts/702")
    assert free.status_code == 200 and free.json()["in_report"] is False

    await client.post("/api/reports/", json={"title": "Занятый", "receiptIds": [702]})
    taken = await client.get("/api/receipts/702")
    assert taken.json()["in_report"] is True


@pytest.mark.asyncio
async def test_receipt_carries_report_name(client, db):
    # Карточке нужно не только «занят», но и КУДА идти: чек лежит ровно
    # в одном отчёте, отцепить его из карточки нельзя.
    await _mk(db, 704, user_id=1)
    created = await client.post(
        "/api/reports/", json={"title": "Июль", "receiptIds": [704]}
    )
    rid = created.json()["id"]

    detail = await client.get("/api/receipts/704")
    assert detail.json()["report_id"] == rid
    assert detail.json()["report_title"] == "Июль"

    listed = await client.get("/api/receipts/")
    item = next(r for r in listed.json() if r["id"] == 704)
    assert item["report_title"] == "Июль"


@pytest.mark.asyncio
async def test_free_receipt_has_no_report_fields(client, db):
    await _mk(db, 705, user_id=1)
    detail = await client.get("/api/receipts/705")
    body = detail.json()
    assert body["in_report"] is False
    assert body["report_id"] is None and body["report_title"] is None


@pytest.mark.asyncio
async def test_patch_receipt_keeps_canonical_shape(client, db):
    # Ответ PATCH подставляется в список на клиенте — форма обязана совпадать,
    # иначе чек «теряет» in_report/report_title (класс бага «0 чеков»).
    await _mk(db, 706, user_id=1)
    await client.post("/api/reports/", json={"title": "Август", "receiptIds": [706]})

    listed = await client.get("/api/receipts/")
    item = next(r for r in listed.json() if r["id"] == 706)

    patched = await client.patch("/api/receipts/706", json={"payment": "Наличные"})
    assert patched.status_code == 200
    assert set(patched.json()) == set(item)
    assert patched.json()["report_title"] == "Август"

    # И на ветке «нечего менять» — тоже канон.
    untouched = await client.patch("/api/receipts/706", json={})
    assert set(untouched.json()) == set(item)


@pytest.mark.asyncio
async def test_receipt_shape_same_in_list_and_detail(client, db):
    # Контракт формы: одиночный чек = элемент списка (оба SELECT * + in_report).
    await _mk(db, 703, user_id=1)
    listed = await client.get("/api/receipts/")
    item = next(r for r in listed.json() if r["id"] == 703)
    detail = await client.get("/api/receipts/703")
    assert set(detail.json()) == set(item)


# ─── REP-ROLES: кто утверждает отчёт ──────────────────────────────────
@pytest.mark.asyncio
async def test_employee_cannot_approve_own_report(client_employee, db):
    # Сотрудник не утверждает собственный отчёт — это контроль расходов.
    await _report(db, 520, user_id=2, status="На проверке")
    resp = await client_employee.patch("/api/reports/520", json={"status": "Одобрен"})
    assert resp.status_code == 403
    assert "бухгалтер" in resp.json()["detail"]
    assert (await db.отчёт(520))["status"] == "На проверке"


@pytest.mark.asyncio
async def test_employee_cannot_reject_report(client_employee, db):
    await _report(db, 521, user_id=2, status="На проверке")
    resp = await client_employee.patch("/api/reports/521", json={"status": "Отклонён"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_employee_can_submit_and_recall_own_report(client_employee, db):
    # «На проверке» (отправить) и «Черновик» (отозвать) автор делает сам.
    await _report(db, 522, user_id=2, status="Черновик")
    sent = await client_employee.patch(
        "/api/reports/522", json={"status": "На проверке"}
    )
    assert sent.status_code == 200 and sent.json()["status"] == "На проверке"
    back = await client_employee.patch("/api/reports/522", json={"status": "Черновик"})
    assert back.status_code == 200 and back.json()["status"] == "Черновик"


@pytest.mark.asyncio
async def test_accountant_approves_employee_report(client_accountant, db):
    await _report(db, 523, user_id=2, status="На проверке")
    resp = await client_accountant.patch("/api/reports/523", json={"status": "Одобрен"})
    assert resp.status_code == 200 and resp.json()["status"] == "Одобрен"


@pytest.mark.asyncio
async def test_admin_approves_report(client, db):
    await _report(db, 524, user_id=2, status="На проверке")
    resp = await client.patch("/api/reports/524", json={"status": "Одобрен"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_employee_approving_foreign_report_403(client_employee, db):
    # Ролевой гейт срабатывает раньше поиска отчёта: 403 говорит о правах
    # действующего лица и не раскрывает, существует ли чужой отчёт.
    await _report(db, 525, user_id=1, status="На проверке")
    resp = await client_employee.patch("/api/reports/525", json={"status": "Одобрен"})
    assert resp.status_code == 403
    assert (await db.отчёт(525))["status"] == "На проверке"


# ─── REP-ACL: видимость отчётов ───────────────────────────────────────
async def _report(db, rid, user_id, *, title="Отчёт", status="Черновик", org_id=ORG):
    await db.добавить_отчёт(
        id=rid,
        title=title,
        user_id=user_id,
        total=0,
        status=status,
        org_id=org_id,
        created=date(2026, 7, 1),
    )


@pytest.mark.asyncio
async def test_employee_sees_only_own_reports(client_employee, db):
    # Сотрудник (id=2) видит свой отчёт и НЕ видит чужой.
    await _report(db, 500, user_id=2, title="Мой")
    await _report(db, 501, user_id=1, title="Чужой")
    resp = await client_employee.get("/api/reports/")
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == [500]


@pytest.mark.asyncio
async def test_admin_sees_all_reports(client, db):
    await _report(db, 502, user_id=2, title="Сотрудника")
    await _report(db, 503, user_id=1, title="Свой")
    resp = await client.get("/api/reports/")
    assert {r["id"] for r in resp.json()} == {502, 503}


@pytest.mark.asyncio
async def test_accountant_sees_all_reports(client_accountant, db):
    # Бухгалтер тоже can_see_all — ему нужно проверять чужие отчёты.
    await _report(db, 504, user_id=2, title="Сотрудника")
    resp = await client_accountant.get("/api/reports/")
    assert [r["id"] for r in resp.json()] == [504]


@pytest.mark.asyncio
async def test_employee_foreign_report_404_on_all_endpoints(client_employee, db):
    # Чужой отчёт неотличим от несуществующего — 404, а не 403.
    await _report(db, 505, user_id=1, title="Чужой")
    await _mk(db, 600, user_id=2)

    assert (await client_employee.get("/api/reports/505")).status_code == 404
    assert (
        await client_employee.patch("/api/reports/505", json={"status": "На проверке"})
    ).status_code == 404
    assert (await client_employee.delete("/api/reports/505")).status_code == 404
    assert (
        await client_employee.post(
            "/api/reports/505/receipts", json={"receiptIds": [600]}
        )
    ).status_code == 404
    assert (
        await client_employee.delete("/api/reports/505/receipts/600")
    ).status_code == 404
    # Чужой отчёт цел и не изменился.
    foreign = await db.отчёт(505)
    assert foreign["status"] == "Черновик"


@pytest.mark.asyncio
async def test_employee_cannot_touch_foreign_report_composition(client_employee, db):
    # Состав чужого отчёта не тронуть даже зная id чека.
    await _report(db, 506, user_id=1)
    await _mk(db, 601, user_id=1)
    await db.положить_в_отчёт(506, 601)
    resp = await client_employee.delete("/api/reports/506/receipts/601")
    assert resp.status_code == 404
    assert {"report_id": 506, "receipt_id": 601} in await db.связи_отчётов()


@pytest.mark.asyncio
async def test_employee_detail_has_no_receiptids_gap(client_employee, db):
    # Следствие REP-ACL (п.4): расхождение receiptIds/receipts из ЧП2 исчезает.
    # Сотрудник видит только СВОИ отчёты, а в них — только свои чеки,
    # поэтому длина receipts всегда равна длине receiptIds.
    await _report(db, 507, user_id=2, title="Мой полный")
    await _mk(db, 602, user_id=2)
    await _mk(db, 603, user_id=2)
    await db.положить_в_отчёт(507, 602)
    await db.положить_в_отчёт(507, 603)

    resp = await client_employee.get("/api/reports/507")
    assert resp.status_code == 200
    body = resp.json()
    assert body["receiptIds"] == [602, 603]
    assert len(body["receipts"]) == len(body["receiptIds"])  # разрыва больше нет


# ─── REP-AUTHOR ЧП3: один отчёт = один подотчётный ────────────────────
@pytest.mark.asyncio
async def test_create_report_own_receipts_ok_invariant(client, db):
    # Свои чеки (автор = создатель, id=1 из фикстуры) — проходят.
    await _mk(db, 400, user_id=1)
    await _mk(db, 401, user_id=1)
    resp = await client.post(
        "/api/reports/", json={"title": "Свои", "receiptIds": [400, 401]}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_create_report_foreign_employee_receipt_409(client, db):
    # Чек ЧУЖОГО сотрудника той же орг: IDOR не срабатывает (org совпадает),
    # но инвариант АО-1 не пускает — иначе непонятно, кому возмещать.
    await _mk(db, 402, user_id=1)
    await _mk(db, 403, user_id=2)  # другой сотрудник той же организации
    resp = await client.post(
        "/api/reports/", json={"title": "Солянка", "receiptIds": [402, 403]}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Чек другого сотрудника — соберите отдельный отчёт"
    assert await db.отчёты() == []  # откат: отчёт не создан


@pytest.mark.asyncio
async def test_create_report_ownerless_receipt_409(client, db):
    # Легаси-чек без владельца (receipts.user_id nullable) — «ничей»,
    # в отчёт не пускаем, иначе инвариант дырявый.
    await _mk(db, 404, user_id=None)
    resp = await client.post(
        "/api/reports/", json={"title": "Ничей", "receiptIds": [404]}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "У чека нет владельца — его нельзя включить в отчёт"
    assert await db.отчёты() == []


@pytest.mark.asyncio
async def test_add_foreign_employee_receipt_to_report_409(client, db):
    # Тот же инвариант на добавлении в существующий отчёт.
    await _mk(db, 405, user_id=1)
    created = await client.post(
        "/api/reports/", json={"title": "Мой", "receiptIds": [405]}
    )
    rid = created.json()["id"]
    await _mk(db, 406, user_id=2)
    resp = await client.post(f"/api/reports/{rid}/receipts", json={"receiptIds": [406]})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Чек другого сотрудника — соберите отдельный отчёт"
    assert 406 not in [с["receipt_id"] for с in await db.связи_отчётов()]


@pytest.mark.asyncio
async def test_add_ownerless_receipt_to_report_409(client, db):
    await _mk(db, 407, user_id=1)
    created = await client.post(
        "/api/reports/", json={"title": "Мой2", "receiptIds": [407]}
    )
    rid = created.json()["id"]
    await _mk(db, 408, user_id=None)
    resp = await client.post(f"/api/reports/{rid}/receipts", json={"receiptIds": [408]})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "У чека нет владельца — его нельзя включить в отчёт"


@pytest.mark.asyncio
async def test_add_receipt_matches_report_author_not_adder(client, db):
    # Эталон — автор ОТЧЁТА, а не тот, кто добавляет. Отчёт сотрудника id=2,
    # добавляет админ id=1: чек автора отчёта пройдёт, чек админа — нет.
    await db.добавить_отчёт(
        id=900,
        title="Отчёт сотрудника",
        user_id=2,
        total=0,
        status="Черновик",
        org_id=1,
        created=date(2026, 7, 1),
    )
    await _mk(db, 409, user_id=2)  # чек автора отчёта
    ok = await client.post("/api/reports/900/receipts", json={"receiptIds": [409]})
    assert ok.status_code == 200

    await _mk(db, 410, user_id=1)  # чек добавляющего админа — чужой для этого отчёта
    bad = await client.post("/api/reports/900/receipts", json={"receiptIds": [410]})
    assert bad.status_code == 409
    assert bad.json()["detail"] == "Чек другого сотрудника — соберите отдельный отчёт"


# ─── REP-CRUD ЧП3: состав отчёта ──────────────────────────────────────
async def _draft_with(client, amounts):
    """Черновик из чеков с указанными суммами → (report_id, [receipt_ids])."""
    ids = []
    for i, amount in enumerate(amounts):
        rc = await client.post(
            "/api/receipts/",
            json={"date": "2026-07-20", "org": f"Орг{i}", "amount": amount},
        )
        ids.append(rc.json()["id"])
    created = await client.post(
        "/api/reports/", json={"title": "Состав", "receiptIds": ids}
    )
    return created.json()["id"], ids


@pytest.mark.asyncio
async def test_add_receipt_updates_ids_and_total(client, db):
    rid, ids = await _draft_with(client, [100.0])
    extra = await client.post(
        "/api/receipts/", json={"date": "2026-07-21", "org": "Ещё", "amount": 50.0}
    )
    resp = await client.post(
        f"/api/reports/{rid}/receipts", json={"receiptIds": [extra.json()["id"]]}
    )
    assert resp.status_code == 200
    assert sorted(resp.json()["receiptIds"]) == sorted(ids + [extra.json()["id"]])
    assert float(resp.json()["total"]) == 150.0  # total пересчитан
    await _сумма_сходится(db, rid)


@pytest.mark.asyncio
async def test_add_receipt_already_in_this_report_is_idempotent(client, db):
    rid, ids = await _draft_with(client, [10.0])
    resp = await client.post(f"/api/reports/{rid}/receipts", json={"receiptIds": ids})
    assert resp.status_code == 200
    assert resp.json()["receiptIds"] == ids  # дубля не появилось
    связи = await db.связи_отчётов()
    assert len([с for с in связи if с["receipt_id"] == ids[0]]) == 1
    assert float(resp.json()["total"]) == 10.0
    await _сумма_сходится(db, rid)


@pytest.mark.asyncio
async def test_add_receipt_from_another_report_409(client):
    rid_a, ids_a = await _draft_with(client, [10.0])
    rid_b, _ = await _draft_with(client, [20.0])
    resp = await client.post(
        f"/api/reports/{rid_b}/receipts", json={"receiptIds": ids_a}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Чек уже в другом отчёте"


@pytest.mark.asyncio
async def test_add_foreign_receipt_403(client, db):
    rid, _ = await _draft_with(client, [10.0])
    await db.добавить_чек(
        id=5000,
        org="Чужая",
        amount=1.0,
        date=date(2026, 7, 22),
        kkt_fn=None,
        org_id=999,
        source="manual",
    )
    resp = await client.post(
        f"/api/reports/{rid}/receipts", json={"receiptIds": [5000]}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_add_receipt_frozen_report_409(client):
    rid, _ = await _draft_with(client, [10.0])
    extra = await client.post(
        "/api/receipts/", json={"date": "2026-07-23", "org": "Х", "amount": 3.0}
    )
    await client.patch(f"/api/reports/{rid}", json={"status": "На проверке"})
    resp = await client.post(
        f"/api/reports/{rid}/receipts", json={"receiptIds": [extra.json()["id"]]}
    )
    assert resp.status_code == 409
    assert "отзовите" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_add_receipt_foreign_report_404(client, db):
    await db.добавить_отчёт(
        id=779,
        title="Чужой",
        user_id=await _чужой_автор(db),
        total=0,
        status="Черновик",
        org_id=999,
        created=date(2026, 7, 1),
    )
    rc = await client.post(
        "/api/receipts/", json={"date": "2026-07-24", "org": "Й", "amount": 1.0}
    )
    resp = await client.post(
        "/api/reports/779/receipts", json={"receiptIds": [rc.json()["id"]]}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_remove_receipt_frees_it_and_recalcs_total(client, db):
    rid, ids = await _draft_with(client, [100.0, 40.0])
    resp = await client.delete(f"/api/reports/{rid}/receipts/{ids[0]}")
    assert resp.status_code == 200
    assert resp.json()["receiptIds"] == [ids[1]]
    assert float(resp.json()["total"]) == 40.0
    await _сумма_сходится(db, rid)
    # Сам чек цел и свободен — его можно положить в другой отчёт.
    assert await db.чек(ids[0]) is not None
    again = await client.post(
        "/api/reports/", json={"title": "Другой", "receiptIds": [ids[0]]}
    )
    assert again.status_code == 200


@pytest.mark.asyncio
async def test_remove_receipt_not_in_report_is_idempotent(client, db):
    rid, ids = await _draft_with(client, [10.0])
    other = await client.post(
        "/api/receipts/",
        json={"date": "2026-07-25", "org": "Не в отчёте", "amount": 9.0},
    )
    resp = await client.delete(f"/api/reports/{rid}/receipts/{other.json()['id']}")
    assert resp.status_code == 200
    assert resp.json()["receiptIds"] == ids  # состав не изменился
    assert float(resp.json()["total"]) == 10.0
    await _сумма_сходится(db, rid)


@pytest.mark.asyncio
async def test_remove_receipt_frozen_report_409(client):
    rid, ids = await _draft_with(client, [10.0])
    await client.patch(f"/api/reports/{rid}", json={"status": "На проверке"})
    await client.patch(f"/api/reports/{rid}", json={"status": "Одобрен"})
    resp = await client.delete(f"/api/reports/{rid}/receipts/{ids[0]}")
    assert resp.status_code == 409
    assert "принят к учёту" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_compose_endpoints_return_list_item_shape(client):
    # Ответы ручек состава — та же форма, что элемент GET-списка.
    rid, ids = await _draft_with(client, [10.0])
    listed = await client.get("/api/reports/")
    item = next(r for r in listed.json() if r["id"] == rid)
    added = await client.post(f"/api/reports/{rid}/receipts", json={"receiptIds": []})
    removed = await client.delete(f"/api/reports/{rid}/receipts/{ids[0]}")
    assert set(added.json()) == set(item)
    assert set(removed.json()) == set(item)


@pytest.mark.asyncio
async def test_delete_report_foreign_org_404(client, db):
    await db.добавить_отчёт(
        id=778,
        title="Чужой",
        user_id=await _чужой_автор(db),
        total=0,
        status="Черновик",
        org_id=999,
        created=date(2026, 7, 1),
    )
    resp = await client.delete("/api/reports/778")
    assert resp.status_code == 404
    assert await db.отчёт(778) is not None  # чужой отчёт не тронут


# ─── REP-CRUD ЧП1: total производный от состава ───────────────────────
@pytest.mark.asyncio
async def test_create_report_total_computed_from_receipts(client, db):
    # total считает БЭК из состава: присланное клиентом значение игнорируется.
    a = await client.post(
        "/api/receipts/", json={"date": "2026-07-01", "org": "А", "amount": 100.0}
    )
    b = await client.post(
        "/api/receipts/", json={"date": "2026-07-02", "org": "Б", "amount": 250.5}
    )
    ids = [a.json()["id"], b.json()["id"]]
    resp = await client.post(
        "/api/reports/",
        json={"title": "Июль", "total": 99999, "receiptIds": ids},  # вранью не верим
    )
    assert resp.status_code == 200
    assert float(resp.json()["total"]) == 350.5
    await _сумма_сходится(db, resp.json()["id"])


@pytest.mark.asyncio
async def test_create_report_without_total_field_ok(client, db):
    # total больше не входит в контракт запроса — без него POST валиден.
    rc = await client.post(
        "/api/receipts/", json={"date": "2026-07-03", "org": "В", "amount": 10.0}
    )
    resp = await client.post(
        "/api/reports/", json={"title": "Без суммы", "receiptIds": [rc.json()["id"]]}
    )
    assert resp.status_code == 200
    assert float(resp.json()["total"]) == 10.0
    await _сумма_сходится(db, resp.json()["id"])


@pytest.mark.asyncio
async def test_report_empty_has_zero_total(client, db):
    resp = await client.post(
        "/api/reports/", json={"title": "Пустой", "receiptIds": []}
    )
    assert resp.status_code == 200
    assert float(resp.json()["total"]) == 0
    await _сумма_сходится(db, resp.json()["id"])


@pytest.mark.asyncio
async def test_receipt_cannot_be_in_two_reports(client, db):
    # Правило «один чек = ровно один отчёт» (uq_report_items_receipt_id):
    # один чек в двух авансовых отчётах = двойное возмещение.
    # ЧП3: сырой UniqueViolationError переведён в дружелюбный 409.
    rc = await client.post(
        "/api/receipts/", json={"date": "2026-07-04", "org": "Г", "amount": 5.0}
    )
    rid = rc.json()["id"]
    first = await client.post(
        "/api/reports/", json={"title": "Первый", "receiptIds": [rid]}
    )
    assert first.status_code == 200
    second = await client.post(
        "/api/reports/", json={"title": "Второй", "receiptIds": [rid]}
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "Чек уже в другом отчёте"
    assert len(await db.отчёты()) == 1  # второй отчёт не создался (откат транзакции)


# ─── S-15: IDOR при создании отчёта — receiptIds скоупятся по org_id ───
@pytest.mark.asyncio
async def test_create_report_own_receipts_ok(client, db):
    # Свои чеки (org_id=1) → 200, report_items записаны.
    now = datetime.utcnow()
    await db.добавить_чек(
        id=10,
        org="X",
        amount=100.0,
        date=date(2026, 6, 1),
        org_id=1,
        user_id=1,
        created_at=now,
    )
    await db.добавить_чек(
        id=11,
        org="Y",
        amount=200.0,
        date=date(2026, 6, 1),
        org_id=1,
        user_id=1,
        created_at=now,
    )
    resp = await client.post(
        "/api/reports/", json={"title": "Июнь", "total": 300.0, "receiptIds": [10, 11]}
    )
    assert resp.status_code == 200
    assert set(resp.json()["receiptIds"]) == {10, 11}
    assert {с["receipt_id"] for с in await db.связи_отчётов()} == {10, 11}
    assert len(await db.отчёты()) == 1


@pytest.mark.asyncio
async def test_create_report_foreign_receipt_403_nothing_written(client, db):
    # Чужой чек (org_id=2) в списке → 403, и отчёт, и позиции откатаны.
    now = datetime.utcnow()
    await db.добавить_чек(
        id=10,
        org="X",
        amount=100.0,
        date=date(2026, 6, 1),
        org_id=1,
        created_at=now,
    )
    await db.добавить_чек(
        id=20,
        org="Чужая",
        amount=50.0,
        date=date(2026, 6, 1),
        org_id=2,
        created_at=now,
    )
    resp = await client.post(
        "/api/reports/", json={"title": "Атака", "total": 150.0, "receiptIds": [10, 20]}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Один или несколько чеков недоступны"
    assert await db.отчёты() == []  # откат: отчёт не появился
    assert await db.связи_отчётов() == []  # откат: позиции не появились


@pytest.mark.asyncio
async def test_create_report_nonexistent_receipt_403(client, db):
    # Несуществующий id 999999 ловится так же (не только чужие, но и фейковые).
    now = datetime.utcnow()
    await db.добавить_чек(
        id=10,
        org="X",
        amount=100.0,
        date=date(2026, 6, 1),
        org_id=1,
        created_at=now,
    )
    resp = await client.post(
        "/api/reports/",
        json={"title": "Фейк", "total": 100.0, "receiptIds": [10, 999999]},
    )
    assert resp.status_code == 403
    assert await db.отчёты() == []
    assert await db.связи_отчётов() == []
