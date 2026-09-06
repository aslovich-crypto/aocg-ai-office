"""API endpoint tests for AOCG AI Office.

Run against an in-memory fake pool (see conftest.py) — no real database is
touched. Each test gets a fresh store via the `db` / `seeded` fixtures.

⚠️ ФАЙЛ ПЕРЕЕЗЖАЕТ НА ЖИВУЮ БАЗУ (T36, перевод по заходам). 06.09.2026 заходом 1
отсюда сняты ручки ЧЕКОВ — 61 тест: список, создание, четыре ветки дедупа,
предупреждения, разбор raw_data в колонки и позиции, PATCH, DELETE, массовое
удаление, подсказка оплаты, источник и фото. Они живут в
`tests/pg/test_receipts_api.py` и идут через настоящий PostgreSQL.

Здесь остались отчёты (заход 2), согласия и справочники (заход 3), карты и
ручка распознавания. Ручка OCR оставлена намеренно: она про разбор ответа
модели, базы почти не касается, и платить за неё подъёмом кластера незачем.
"""

from datetime import date, datetime


from app.routers.consent import POLICY_VERSION


# ⚠️ ОСТАЁТСЯ ЗДЕСЬ ДО ЗАХОДА 2. Помощник заводился для массового удаления
# чеков (уехало на живую базу заходом 1), но им пользуются и тесты СОСТАВА
# ОТЧЁТА, которые ещё живут на двойнике. Снести его вместе с чеками значило бы
# уронить шестнадцать чужих тестов.
def _mk(db, rid, *, source="manual", kkt_fn=None, org_id=1, amount=100.0, user_id=1):
    db.receipts.append(
        dict(
            id=rid,
            date=date(2026, 5, 20),
            org=f"Org{rid}",
            # колонка receipts.category дропнута с прода (канон — category_id):
            # держать её в фейке нельзя, иначе тестовая строка богаче реальной.
            payment=None,
            amount=amount,
            employee=None,
            kkt_fn=kkt_fn,
            raw_data=None,
            source=source,
            photo_url=None,
            org_id=org_id,
            user_id=user_id,  # REP-AUTHOR: у чека всегда есть владелец
            created_at=datetime.utcnow(),
        )
    )
    db._rid = max(db._rid, rid)


# ─── GET /api/reports/ ────────────────────────────────────────────────
async def test_get_reports_returns_list(client):
    resp = await client.get("/api/reports/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ─── POST /api/reports/ ───────────────────────────────────────────────
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
async def test_patch_report_status(client, seeded):
    resp = await client.patch("/api/reports/1", json={"status": "На проверке"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "На проверке"


async def test_patch_report_status_invalid_rejected(client, seeded):
    # Статус вне жизненного цикла (в т.ч. старый 'Личные') — 422, не проходит.
    resp = await client.patch("/api/reports/1", json={"status": "Личные"})
    assert resp.status_code == 422


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
async def test_get_report_detail_not_found(client):
    resp = await client.get("/api/reports/99999")
    assert resp.status_code == 404


async def test_get_report_detail_foreign_org_404(client, db):
    # Чужой отчёт неотличим от несуществующего.
    db.reports.append(
        dict(
            id=777,
            title="Чужой",
            status="Черновик",
            total=0,
            org_id=999,
            created=date(2026, 7, 1),
            created_at=datetime.utcnow(),
        )
    )
    resp = await client.get("/api/reports/777")
    assert resp.status_code == 404


async def test_get_report_detail_empty_report(client):
    created = await client.post(
        "/api/reports/", json={"title": "Пустой", "receiptIds": []}
    )
    resp = await client.get(f"/api/reports/{created.json()['id']}")
    assert resp.status_code == 200
    assert resp.json()["receiptIds"] == [] and resp.json()["receipts"] == []


# ─── REP-CRUD ЧП2: DELETE /{id} ───────────────────────────────────────
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
    assert all(r["id"] != rid for r in db.reports)
    # Состав ушёл каскадом, сам чек остался и снова свободен.
    assert all(ri["report_id"] != rid for ri in db.report_items)
    assert any(r["id"] == rc.json()["id"] for r in db.receipts)


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
    assert all(r["id"] != rid for r in db.reports)


async def test_delete_report_in_review_409_says_recall_first(client, db):
    created = await client.post(
        "/api/reports/", json={"title": "На проверке", "receiptIds": []}
    )
    rid = created.json()["id"]
    await client.patch(f"/api/reports/{rid}", json={"status": "На проверке"})

    resp = await client.delete(f"/api/reports/{rid}")
    assert resp.status_code == 409
    assert "отзовите" in resp.json()["detail"]  # текст объясняет следующий шаг
    assert any(r["id"] == rid for r in db.reports)  # отчёт на месте


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
    assert any(r["id"] == rid for r in db.reports)


# ─── REP-AUTHOR ЧП1: автор отчёта ─────────────────────────────────────
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
    stored = next(r for r in db.reports if r["id"] == resp.json()["id"])
    assert stored["user_id"] == 1


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


async def test_shape_contract_reports(client, db):
    """T7: каждая ручка отчёта отдаёт форму элемента GET /api/reports/."""
    _mk(db, 810, user_id=1)
    _mk(db, 811, user_id=1)
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
async def test_receipts_list_has_in_report_flag(client, db):
    # Кнопке «Прикрепить к отчёту» нужно знать, свободен ли чек.
    _mk(db, 700, user_id=1)  # свободный
    _mk(db, 701, user_id=1)  # уйдёт в отчёт
    await client.post("/api/reports/", json={"title": "С чеком", "receiptIds": [701]})

    resp = await client.get("/api/receipts/")
    assert resp.status_code == 200
    by_id = {r["id"]: r for r in resp.json()}
    assert by_id[701]["in_report"] is True
    assert by_id[700]["in_report"] is False


async def test_receipt_detail_has_in_report_flag(client, db):
    # Карточку открывают и напрямую — форма чека не должна зависеть от пути.
    _mk(db, 702, user_id=1)
    free = await client.get("/api/receipts/702")
    assert free.status_code == 200 and free.json()["in_report"] is False

    await client.post("/api/reports/", json={"title": "Занятый", "receiptIds": [702]})
    taken = await client.get("/api/receipts/702")
    assert taken.json()["in_report"] is True


async def test_receipt_carries_report_name(client, db):
    # Карточке нужно не только «занят», но и КУДА идти: чек лежит ровно
    # в одном отчёте, отцепить его из карточки нельзя.
    _mk(db, 704, user_id=1)
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


async def test_free_receipt_has_no_report_fields(client, db):
    _mk(db, 705, user_id=1)
    detail = await client.get("/api/receipts/705")
    body = detail.json()
    assert body["in_report"] is False
    assert body["report_id"] is None and body["report_title"] is None


async def test_patch_receipt_keeps_canonical_shape(client, db):
    # Ответ PATCH подставляется в список на клиенте — форма обязана совпадать,
    # иначе чек «теряет» in_report/report_title (класс бага «0 чеков»).
    _mk(db, 706, user_id=1)
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


async def test_receipt_shape_same_in_list_and_detail(client, db):
    # Контракт формы: одиночный чек = элемент списка (оба SELECT * + in_report).
    _mk(db, 703, user_id=1)
    listed = await client.get("/api/receipts/")
    item = next(r for r in listed.json() if r["id"] == 703)
    detail = await client.get("/api/receipts/703")
    assert set(detail.json()) == set(item)


# ─── REP-ROLES: кто утверждает отчёт ──────────────────────────────────
async def test_employee_cannot_approve_own_report(client_employee, db):
    # Сотрудник не утверждает собственный отчёт — это контроль расходов.
    _report(db, 520, user_id=2, status="На проверке")
    resp = await client_employee.patch("/api/reports/520", json={"status": "Одобрен"})
    assert resp.status_code == 403
    assert "бухгалтер" in resp.json()["detail"]
    assert next(r for r in db.reports if r["id"] == 520)["status"] == "На проверке"


async def test_employee_cannot_reject_report(client_employee, db):
    _report(db, 521, user_id=2, status="На проверке")
    resp = await client_employee.patch("/api/reports/521", json={"status": "Отклонён"})
    assert resp.status_code == 403


async def test_employee_can_submit_and_recall_own_report(client_employee, db):
    # «На проверке» (отправить) и «Черновик» (отозвать) автор делает сам.
    _report(db, 522, user_id=2, status="Черновик")
    sent = await client_employee.patch(
        "/api/reports/522", json={"status": "На проверке"}
    )
    assert sent.status_code == 200 and sent.json()["status"] == "На проверке"
    back = await client_employee.patch("/api/reports/522", json={"status": "Черновик"})
    assert back.status_code == 200 and back.json()["status"] == "Черновик"


async def test_accountant_approves_employee_report(client_accountant, db):
    _report(db, 523, user_id=2, status="На проверке")
    resp = await client_accountant.patch("/api/reports/523", json={"status": "Одобрен"})
    assert resp.status_code == 200 and resp.json()["status"] == "Одобрен"


async def test_admin_approves_report(client, db):
    _report(db, 524, user_id=2, status="На проверке")
    resp = await client.patch("/api/reports/524", json={"status": "Одобрен"})
    assert resp.status_code == 200


async def test_employee_approving_foreign_report_403(client_employee, db):
    # Ролевой гейт срабатывает раньше поиска отчёта: 403 говорит о правах
    # действующего лица и не раскрывает, существует ли чужой отчёт.
    _report(db, 525, user_id=1, status="На проверке")
    resp = await client_employee.patch("/api/reports/525", json={"status": "Одобрен"})
    assert resp.status_code == 403
    assert next(r for r in db.reports if r["id"] == 525)["status"] == "На проверке"


# ─── REP-ACL: видимость отчётов ───────────────────────────────────────
def _report(db, rid, user_id, *, title="Отчёт", status="Черновик", org_id=1):
    db.reports.append(
        dict(
            id=rid,
            title=title,
            status=status,
            total=0,
            org_id=org_id,
            user_id=user_id,
            created=date(2026, 7, 1),
            created_at=datetime.utcnow(),
        )
    )
    db._repid = max(db._repid, rid)


async def test_employee_sees_only_own_reports(client_employee, db):
    # Сотрудник (id=2) видит свой отчёт и НЕ видит чужой.
    _report(db, 500, user_id=2, title="Мой")
    _report(db, 501, user_id=1, title="Чужой")
    resp = await client_employee.get("/api/reports/")
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == [500]


async def test_admin_sees_all_reports(client, db):
    _report(db, 502, user_id=2, title="Сотрудника")
    _report(db, 503, user_id=1, title="Свой")
    resp = await client.get("/api/reports/")
    assert {r["id"] for r in resp.json()} == {502, 503}


async def test_accountant_sees_all_reports(client_accountant, db):
    # Бухгалтер тоже can_see_all — ему нужно проверять чужие отчёты.
    _report(db, 504, user_id=2, title="Сотрудника")
    resp = await client_accountant.get("/api/reports/")
    assert [r["id"] for r in resp.json()] == [504]


async def test_employee_foreign_report_404_on_all_endpoints(client_employee, db):
    # Чужой отчёт неотличим от несуществующего — 404, а не 403.
    _report(db, 505, user_id=1, title="Чужой")
    _mk(db, 600, user_id=2)

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
    foreign = next(r for r in db.reports if r["id"] == 505)
    assert foreign["status"] == "Черновик"


async def test_employee_cannot_touch_foreign_report_composition(client_employee, db):
    # Состав чужого отчёта не тронуть даже зная id чека.
    _report(db, 506, user_id=1)
    _mk(db, 601, user_id=1)
    db.report_items.append({"report_id": 506, "receipt_id": 601})
    resp = await client_employee.delete("/api/reports/506/receipts/601")
    assert resp.status_code == 404
    assert {"report_id": 506, "receipt_id": 601} in db.report_items


async def test_employee_detail_has_no_receiptids_gap(client_employee, db):
    # Следствие REP-ACL (п.4): расхождение receiptIds/receipts из ЧП2 исчезает.
    # Сотрудник видит только СВОИ отчёты, а в них — только свои чеки,
    # поэтому длина receipts всегда равна длине receiptIds.
    _report(db, 507, user_id=2, title="Мой полный")
    _mk(db, 602, user_id=2)
    _mk(db, 603, user_id=2)
    db.report_items.append({"report_id": 507, "receipt_id": 602})
    db.report_items.append({"report_id": 507, "receipt_id": 603})

    resp = await client_employee.get("/api/reports/507")
    assert resp.status_code == 200
    body = resp.json()
    assert body["receiptIds"] == [602, 603]
    assert len(body["receipts"]) == len(body["receiptIds"])  # разрыва больше нет


# ─── REP-AUTHOR ЧП3: один отчёт = один подотчётный ────────────────────
async def test_create_report_own_receipts_ok_invariant(client, db):
    # Свои чеки (автор = создатель, id=1 из фикстуры) — проходят.
    _mk(db, 400, user_id=1)
    _mk(db, 401, user_id=1)
    resp = await client.post(
        "/api/reports/", json={"title": "Свои", "receiptIds": [400, 401]}
    )
    assert resp.status_code == 200


async def test_create_report_foreign_employee_receipt_409(client, db):
    # Чек ЧУЖОГО сотрудника той же орг: IDOR не срабатывает (org совпадает),
    # но инвариант АО-1 не пускает — иначе непонятно, кому возмещать.
    _mk(db, 402, user_id=1)
    _mk(db, 403, user_id=2)  # другой сотрудник той же организации
    resp = await client.post(
        "/api/reports/", json={"title": "Солянка", "receiptIds": [402, 403]}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Чек другого сотрудника — соберите отдельный отчёт"
    assert db.reports == []  # откат: отчёт не создан


async def test_create_report_ownerless_receipt_409(client, db):
    # Легаси-чек без владельца (receipts.user_id nullable) — «ничей»,
    # в отчёт не пускаем, иначе инвариант дырявый.
    _mk(db, 404, user_id=None)
    resp = await client.post(
        "/api/reports/", json={"title": "Ничей", "receiptIds": [404]}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "У чека нет владельца — его нельзя включить в отчёт"
    assert db.reports == []


async def test_add_foreign_employee_receipt_to_report_409(client, db):
    # Тот же инвариант на добавлении в существующий отчёт.
    _mk(db, 405, user_id=1)
    created = await client.post(
        "/api/reports/", json={"title": "Мой", "receiptIds": [405]}
    )
    rid = created.json()["id"]
    _mk(db, 406, user_id=2)
    resp = await client.post(f"/api/reports/{rid}/receipts", json={"receiptIds": [406]})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Чек другого сотрудника — соберите отдельный отчёт"
    assert 406 not in [ri["receipt_id"] for ri in db.report_items]


async def test_add_ownerless_receipt_to_report_409(client, db):
    _mk(db, 407, user_id=1)
    created = await client.post(
        "/api/reports/", json={"title": "Мой2", "receiptIds": [407]}
    )
    rid = created.json()["id"]
    _mk(db, 408, user_id=None)
    resp = await client.post(f"/api/reports/{rid}/receipts", json={"receiptIds": [408]})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "У чека нет владельца — его нельзя включить в отчёт"


async def test_add_receipt_matches_report_author_not_adder(client, db):
    # Эталон — автор ОТЧЁТА, а не тот, кто добавляет. Отчёт сотрудника id=2,
    # добавляет админ id=1: чек автора отчёта пройдёт, чек админа — нет.
    db.reports.append(
        dict(
            id=900,
            title="Отчёт сотрудника",
            status="Черновик",
            total=0,
            org_id=1,
            user_id=2,
            created=date(2026, 7, 1),
            created_at=datetime.utcnow(),
        )
    )
    db._repid = 900
    _mk(db, 409, user_id=2)  # чек автора отчёта
    ok = await client.post("/api/reports/900/receipts", json={"receiptIds": [409]})
    assert ok.status_code == 200

    _mk(db, 410, user_id=1)  # чек добавляющего админа — чужой для этого отчёта
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


async def test_add_receipt_updates_ids_and_total(client):
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


async def test_add_receipt_already_in_this_report_is_idempotent(client, db):
    rid, ids = await _draft_with(client, [10.0])
    resp = await client.post(f"/api/reports/{rid}/receipts", json={"receiptIds": ids})
    assert resp.status_code == 200
    assert resp.json()["receiptIds"] == ids  # дубля не появилось
    assert len([ri for ri in db.report_items if ri["receipt_id"] == ids[0]]) == 1
    assert float(resp.json()["total"]) == 10.0


async def test_add_receipt_from_another_report_409(client):
    rid_a, ids_a = await _draft_with(client, [10.0])
    rid_b, _ = await _draft_with(client, [20.0])
    resp = await client.post(
        f"/api/reports/{rid_b}/receipts", json={"receiptIds": ids_a}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Чек уже в другом отчёте"


async def test_add_foreign_receipt_403(client, db):
    rid, _ = await _draft_with(client, [10.0])
    db.receipts.append(
        dict(
            id=5000,
            date=date(2026, 7, 22),
            org="Чужая",
            amount=1.0,
            org_id=999,
            source="manual",
            kkt_fn=None,
        )
    )
    resp = await client.post(
        f"/api/reports/{rid}/receipts", json={"receiptIds": [5000]}
    )
    assert resp.status_code == 403


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


async def test_add_receipt_foreign_report_404(client, db):
    db.reports.append(
        dict(
            id=779,
            title="Чужой",
            status="Черновик",
            total=0,
            org_id=999,
            created=date(2026, 7, 1),
            created_at=datetime.utcnow(),
        )
    )
    rc = await client.post(
        "/api/receipts/", json={"date": "2026-07-24", "org": "Й", "amount": 1.0}
    )
    resp = await client.post(
        "/api/reports/779/receipts", json={"receiptIds": [rc.json()["id"]]}
    )
    assert resp.status_code == 404


async def test_remove_receipt_frees_it_and_recalcs_total(client, db):
    rid, ids = await _draft_with(client, [100.0, 40.0])
    resp = await client.delete(f"/api/reports/{rid}/receipts/{ids[0]}")
    assert resp.status_code == 200
    assert resp.json()["receiptIds"] == [ids[1]]
    assert float(resp.json()["total"]) == 40.0
    # Сам чек цел и свободен — его можно положить в другой отчёт.
    assert any(r["id"] == ids[0] for r in db.receipts)
    again = await client.post(
        "/api/reports/", json={"title": "Другой", "receiptIds": [ids[0]]}
    )
    assert again.status_code == 200


async def test_remove_receipt_not_in_report_is_idempotent(client):
    rid, ids = await _draft_with(client, [10.0])
    other = await client.post(
        "/api/receipts/",
        json={"date": "2026-07-25", "org": "Не в отчёте", "amount": 9.0},
    )
    resp = await client.delete(f"/api/reports/{rid}/receipts/{other.json()['id']}")
    assert resp.status_code == 200
    assert resp.json()["receiptIds"] == ids  # состав не изменился
    assert float(resp.json()["total"]) == 10.0


async def test_remove_receipt_frozen_report_409(client):
    rid, ids = await _draft_with(client, [10.0])
    await client.patch(f"/api/reports/{rid}", json={"status": "На проверке"})
    await client.patch(f"/api/reports/{rid}", json={"status": "Одобрен"})
    resp = await client.delete(f"/api/reports/{rid}/receipts/{ids[0]}")
    assert resp.status_code == 409
    assert "принят к учёту" in resp.json()["detail"]


async def test_compose_endpoints_return_list_item_shape(client):
    # Ответы ручек состава — та же форма, что элемент GET-списка.
    rid, ids = await _draft_with(client, [10.0])
    listed = await client.get("/api/reports/")
    item = next(r for r in listed.json() if r["id"] == rid)
    added = await client.post(f"/api/reports/{rid}/receipts", json={"receiptIds": []})
    removed = await client.delete(f"/api/reports/{rid}/receipts/{ids[0]}")
    assert set(added.json()) == set(item)
    assert set(removed.json()) == set(item)


async def test_delete_report_foreign_org_404(client, db):
    db.reports.append(
        dict(
            id=778,
            title="Чужой",
            status="Черновик",
            total=0,
            org_id=999,
            created=date(2026, 7, 1),
            created_at=datetime.utcnow(),
        )
    )
    resp = await client.delete("/api/reports/778")
    assert resp.status_code == 404
    assert any(r["id"] == 778 for r in db.reports)  # чужой отчёт не тронут


# ─── REP-CRUD ЧП1: total производный от состава ───────────────────────
async def test_create_report_total_computed_from_receipts(client):
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


async def test_create_report_without_total_field_ok(client):
    # total больше не входит в контракт запроса — без него POST валиден.
    rc = await client.post(
        "/api/receipts/", json={"date": "2026-07-03", "org": "В", "amount": 10.0}
    )
    resp = await client.post(
        "/api/reports/", json={"title": "Без суммы", "receiptIds": [rc.json()["id"]]}
    )
    assert resp.status_code == 200
    assert float(resp.json()["total"]) == 10.0


async def test_report_empty_has_zero_total(client):
    resp = await client.post(
        "/api/reports/", json={"title": "Пустой", "receiptIds": []}
    )
    assert resp.status_code == 200
    assert float(resp.json()["total"]) == 0


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
    assert len(db.reports) == 1  # второй отчёт не создался (откат транзакции)


# ─── GET /api/cards/ ──────────────────────────────────────────────────
async def test_get_cards_returns_list(client, seeded):
    resp = await client.get("/api/cards/")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["name"] == "Корп.карта"


# ─── POST /api/cards/ ─────────────────────────────────────────────────
async def test_create_card(client):
    resp = await client.post("/api/cards/", json={"name": "Личная Сбер"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] > 0
    assert body["name"] == "Личная Сбер"


# ─── DELETE /api/cards/{id} ───────────────────────────────────────────
async def test_delete_card(client):
    created = await client.post("/api/cards/", json={"name": "Временная"})
    cid = created.json()["id"]

    resp = await client.delete(f"/api/cards/{cid}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    remaining = (await client.get("/api/cards/")).json()
    assert all(c["id"] != cid for c in remaining)


# ─── POST /api/receipts/ocr/ ──────────────────────────────────────────
# A 1×1 PNG — anything we'd actually OCR is too big to inline, and the
# Anthropic client is mocked end-to-end so the image bytes never reach it.
import base64
import io

from anthropic import APITimeoutError

import app.routers.ocr as ocr_module

_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class _Block:
    """Minimal stand-in for an Anthropic text content block."""

    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeMessages:
    def __init__(self, behavior):
        self._behavior = behavior

    async def create(self, **kwargs):
        return self._behavior(kwargs)


class _FakeClient:
    """Stand-in for AsyncAnthropic.with_options(...) result."""

    def __init__(self, behavior):
        self.messages = _FakeMessages(behavior)


def _install_fake(monkeypatch, behavior):
    """Replace the module-level Anthropic client with one that runs `behavior`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class _Stub:
        def with_options(self, **_):
            return _FakeClient(behavior)

    monkeypatch.setattr(ocr_module, "_anthropic_client", _Stub())


async def test_ocr_rejects_non_image(client):
    files = {"file": ("note.txt", io.BytesIO(b"hello"), "text/plain")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 400
    assert "Unsupported" in resp.json()["detail"]


async def test_ocr_rejects_oversized_file(client):
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 10)
    files = {"file": ("big.png", io.BytesIO(big), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 400
    assert "exceeds" in resp.json()["detail"].lower()


async def test_ocr_rejects_empty_file(client):
    files = {"file": ("empty.png", io.BytesIO(b""), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 400


async def test_ocr_happy_path(client, monkeypatch):
    payload = {
        "org_legal": 'ООО "Тандер"',
        "org_brand": "Магнит",
        "org_inn": "7707083893",
        "address": "Москва",
        "datetime": "2026-05-15T13:42:00",
        "amount": 1234.56,
        "operation_type": "purchase",
        "payment_form": "card",
        "tax_system": "usn_income",
        "vat_20": 123.45,
        "items": [
            {
                "position": 1,
                "name": "Молоко",
                "quantity": 1,
                "price": 89.0,
                "sum": 89.0,
                "vat_rate": "20",
            }
        ],
        "confidence": "high",
    }
    import json as _json

    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["org_brand"] == "Магнит"
    assert body["org"] == "Магнит"  # alias: org_brand or org_legal
    assert body["amount"] == 1234.56
    # auto-categorization v2 picks up "Магнит" → "Продукты для офиса"
    assert body["category"] == "Продукты для офиса"


async def test_ocr_strips_markdown_fences(client, monkeypatch):
    """Claude sometimes wraps JSON in ```json ... ``` despite the prompt."""
    wrapped = (
        '```json\n{"org_brand": "Лукойл", "amount": 3000, "confidence": "medium"}\n```'
    )
    _install_fake(monkeypatch, lambda kw: _Response(wrapped))

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["org"] == "Лукойл"
    assert body["category"] == "Топливо"


async def test_ocr_timeout_returns_low_confidence(client, monkeypatch):
    def boom(_kw):
        raise APITimeoutError(request=None)

    _install_fake(monkeypatch, boom)

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    # User said: timeout / unreadable -> low-confidence object, NOT 500.
    assert resp.status_code == 200
    body = resp.json()
    assert body["confidence"] == "low"
    assert body["amount"] is None
    assert body["org"] is None


async def test_ocr_garbage_response_returns_low_confidence(client, monkeypatch):
    _install_fake(monkeypatch, lambda kw: _Response("sorry, I cannot read this"))

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200
    assert resp.json()["confidence"] == "low"


async def test_ocr_missing_api_key_returns_low_confidence(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Don't install a fake — we shouldn't reach the client at all.
    monkeypatch.setattr(ocr_module, "_anthropic_client", None)

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200
    assert resp.json()["confidence"] == "low"


# ─── ЧП E: new-standard OCR fields + backward-compat aliases ──────────
async def test_ocr_aliases_backward_compat(client, monkeypatch):
    """New rich response from Claude → the old aliases the frontend reads exist."""
    payload = {
        "org_legal": 'ООО "Денежные энергии"',
        "org_brand": "Aster",
        "org_inn": "7707083893",
        "address": "СПб, Невский 1",
        "datetime": "2026-05-21T12:17:00",
        "amount": 6660.0,
        "currency": "RUB",
        "operation_type": "purchase",
        "payment_form": "card",
        "payment_detail": "Корпоративная 3950",
        "card_last4": "3950",
        "tax_system": "usn_income",
        "vat_20": 1110.0,
        "vat_10": None,
        "vat_0": 5550.0,
        "cashier": "Дробушков Никита",
        "items": [
            {
                "position": 1,
                "name": "Шакшука",
                "quantity": 1.0,
                "price": 750.0,
                "sum": 750.0,
                "vat_rate": "20",
            }
        ],
        "confidence": "high",
    }
    import json as _json

    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    body = (await client.post("/api/receipts/ocr/", files=files)).json()

    # rich fields preserved
    assert body["org_brand"] == "Aster"
    assert body["tax_system"] == "usn_income"
    assert body["vat_0"] == 5550.0
    # backward-compat aliases the current frontend (handleOcrFile) reads
    assert body["org"] == "Aster"  # org_brand or org_legal
    assert body["amount"] == 6660.0
    assert body["date"] == "2026-05-21"  # from datetime
    assert body["time"] == "12:17:00"
    assert body["payment_type"] == "card"  # from payment_form
    assert body["inn"] == "7707083893"  # alias of org_inn
    assert body["category"]  # auto-categorized from org
    assert body["nds"] == 1110.0  # vat_20 + vat_10(None)
    assert body["items"][0]["total"] == 750.0  # sum aliased to total


async def test_ocr_invalid_inn_returns_null(client, monkeypatch):
    """An OCR-misread INN with a bad checksum is dropped + a warning is added."""
    payload = {
        "org_brand": "Лавка",
        "amount": 100.0,
        "org_inn": "1234567890",
        "confidence": "high",
    }
    import json as _json

    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    body = (await client.post("/api/receipts/ocr/", files=files)).json()
    assert body["org_inn"] is None
    assert body["inn"] is None
    assert any("ИНН" in w for w in body["warnings"])


async def test_ocr_datetime_formats(client, monkeypatch):
    """Assorted human datetime formats normalize to ISO; junk → None."""
    import json as _json

    cases = {
        "2026-05-21T12:17:00": "2026-05-21T12:17:00",
        "21.05.2026 12:17": "2026-05-21T12:17:00",
        "21.05.2026": "2026-05-21T00:00:00",
        "2026-05-21": "2026-05-21T00:00:00",
        "не дата": None,
    }
    for raw, expected in cases.items():
        payload = {
            "org_brand": "X",
            "amount": 1.0,
            "datetime": raw,
            "confidence": "high",
        }
        _install_fake(monkeypatch, lambda kw, p=payload: _Response(_json.dumps(p)))
        files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
        body = (await client.post("/api/receipts/ocr/", files=files)).json()
        assert body["datetime"] == expected, f"{raw!r} → {body['datetime']!r}"


async def test_ocr_partial_response_fallback(client, monkeypatch):
    """No org / no amount → aliases are None, so the frontend shows 'partial'."""
    payload = {"address": "СПб", "confidence": "low"}  # neither org nor amount
    import json as _json

    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    body = (await client.post("/api/receipts/ocr/", files=files)).json()
    assert body["org"] is None  # frontend: !d.org → "partial"
    assert body["amount"] is None


async def test_ocr_no_fiscal_fields_requested(client, monkeypatch):
    """Промпт не просит РЕКВИЗИТЫ — но просит признак документа (№25, Б).

    ⚠️ УТВЕРЖДЕНИЕ ИЗМЕНИЛОСЬ 04.09.2026, И ЭТО РЕШЕНИЕ ВЛАДЕЛЬЦА, А НЕ
    ПОСЛАБЛЕНИЕ. Было: «не проси ничего фискального». Стало: ФН, ЗН и РН
    по-прежнему нельзя — опечатка в них портит реквизит; а ФД и ФПД просим
    ОТДЕЛЬНЫМИ ключами `ocr_fd`/`ocr_fpd`, которые в карточку чека не
    попадают и реквизитами не считаются. Ошибка в цифре тогда даёт
    «не дубль», а не ложь в документе.

    Сторож остался сторожем: он всё так же требует, чтобы модель НЕ
    заполняла настоящие `fd_num`/`fpd` — иначе распознанное поехало бы
    в реквизиты той же дорогой, что и раньше.
    """
    captured = {}

    def capture(kw):
        captured["prompt"] = kw["messages"][0]["content"][1]["text"]
        return _Response('{"org_brand": "X", "amount": 1, "confidence": "high"}')

    _install_fake(monkeypatch, capture)
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    await client.post("/api/receipts/ocr/", files=files)
    prompt = captured["prompt"]
    for key in ("kkt_fn", "kkt_rn", "kkt_serial", "fiscalDriveNumber"):
        assert key not in prompt, f"{key} — реквизит, распознаванию не отдаём"
    for реквизит in ('"fd_num"', '"fpd"'):
        assert реквизит not in prompt, (
            f"{реквизит} — колонка реквизита; модель заполнять её не должна"
        )
    for признак in ('"ocr_fd"', '"ocr_fpd"'):
        assert признак in prompt, f"{признак} нужен для поиска повторного фото"


# ─── POST /api/consent/ ───────────────────────────────────────────────
# СТРОКА 9: субъект берётся ИЗ ТОКЕНА, адрес — ИЗ ЗАПРОСА. Клиент не может
# быть источником доказательства о самом себе, поэтому тела эти поля больше
# не несут (а если старый фронт их пришлёт — они игнорируются).
async def test_post_consent_records_row(client, db):
    resp = await client.post("/api/consent/", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] > 0
    # S-34: версия берётся из ИСТОЧНИКА, а не литералом — иначе тест
    # становится третьей копией того же значения и расходится с ним.
    assert body["policy_version"] == POLICY_VERSION
    assert body["consent_at"] is not None
    assert len(db.consents) == 1
    # id=1 — это подменённый get_current_user в фикстуре client.
    assert db.consents[0]["user_id"] == "1"
    assert "Шукалович" in db.consents[0]["consent_text"]


async def test_post_consent_ignores_subject_from_body(client, db):
    """Подсунуть чужой user_id через тело нельзя — иначе запись подделывается.

    Именно так и появились девятнадцать легаси-строк «local_user»: значение
    приходило от клиента, и журнал не опознаёт по ним никого.
    """
    resp = await client.post(
        "/api/consent/",
        json={"user_id": "local_user", "ip_address": "203.0.113.4"},
    )
    assert resp.status_code == 200
    assert db.consents[0]["user_id"] == "1", "субъект обязан приходить из токена"
    assert db.consents[0]["ip_address"] != "203.0.113.4", (
        "адрес обязан браться из запроса, а не из тела"
    )


async def test_post_consent_records_client_address(client, db):
    """Адрес пишется сервером. В тестах соединение локальное — важно, что
    поле ЗАПОЛНЕНО и взято не из тела."""
    await client.post("/api/consent/", json={})
    assert db.consents[0]["ip_address"], "адрес обязан проставиться"


async def test_post_consent_appends_on_reagree(client, db):
    """Re-agreement is intentional — we append rather than upsert."""
    await client.post("/api/consent/", json={})
    await client.post("/api/consent/", json={})
    assert len(db.consents) == 2


# ─── GET /api/consent/{user_id} ───────────────────────────────────────
async def test_get_consent_returns_null_when_none(client):
    resp = await client.get("/api/consent/never_consented")
    assert resp.status_code == 200
    assert resp.json() is None


async def test_get_consent_returns_latest(client, db):
    await client.post("/api/consent/", json={})
    second = await client.post("/api/consent/", json={})
    resp = await client.get("/api/consent/1")
    assert resp.status_code == 200
    body = resp.json()
    # 'latest' = highest id, which the POST returned
    assert body["id"] == second.json()["id"]
    # S-34: версия берётся из ИСТОЧНИКА, а не литералом — иначе тест
    # становится третьей копией того же значения и расходится с ним.
    assert body["policy_version"] == POLICY_VERSION


async def test_get_consent_isolates_users(client, db):
    await client.post("/api/consent/", json={})
    resp = await client.get("/api/consent/bob")
    assert resp.status_code == 200
    assert resp.json() is None


# ─── S-15: IDOR при создании отчёта — receiptIds скоупятся по org_id ───
async def test_create_report_own_receipts_ok(client, db):
    # Свои чеки (org_id=1) → 200, report_items записаны.
    now = datetime.utcnow()
    db.receipts.append(
        dict(
            id=10,
            date=date(2026, 6, 1),
            org="X",
            amount=100.0,
            org_id=1,
            user_id=1,  # REP-AUTHOR: чек принадлежит создателю отчёта
            created_at=now,
        )
    )
    db.receipts.append(
        dict(
            id=11,
            date=date(2026, 6, 1),
            org="Y",
            amount=200.0,
            org_id=1,
            user_id=1,
            created_at=now,
        )
    )
    resp = await client.post(
        "/api/reports/", json={"title": "Июнь", "total": 300.0, "receiptIds": [10, 11]}
    )
    assert resp.status_code == 200
    assert set(resp.json()["receiptIds"]) == {10, 11}
    assert {ri["receipt_id"] for ri in db.report_items} == {10, 11}
    assert len(db.reports) == 1


async def test_create_report_foreign_receipt_403_nothing_written(client, db):
    # Чужой чек (org_id=2) в списке → 403, и отчёт, и позиции откатаны.
    now = datetime.utcnow()
    db.receipts.append(
        dict(
            id=10,
            date=date(2026, 6, 1),
            org="X",
            amount=100.0,
            org_id=1,
            created_at=now,
        )
    )
    db.receipts.append(
        dict(
            id=20,
            date=date(2026, 6, 1),
            org="Чужая",
            amount=50.0,
            org_id=2,
            created_at=now,
        )
    )
    resp = await client.post(
        "/api/reports/", json={"title": "Атака", "total": 150.0, "receiptIds": [10, 20]}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Один или несколько чеков недоступны"
    assert db.reports == []  # откат: отчёт не появился
    assert db.report_items == []  # откат: позиции не появились


async def test_create_report_nonexistent_receipt_403(client, db):
    # Несуществующий id 999999 ловится так же (не только чужие, но и фейковые).
    now = datetime.utcnow()
    db.receipts.append(
        dict(
            id=10,
            date=date(2026, 6, 1),
            org="X",
            amount=100.0,
            org_id=1,
            created_at=now,
        )
    )
    resp = await client.post(
        "/api/reports/",
        json={"title": "Фейк", "total": 100.0, "receiptIds": [10, 999999]},
    )
    assert resp.status_code == 403
    assert db.reports == []
    assert db.report_items == []


# ─── строка 24: неправдоподобная дата попадает в warnings ручки OCR ───
async def test_ocr_warns_about_implausible_date(client, monkeypatch):
    """Ровно случай 12.08.2026: модель вернула 2024 год на сегодняшний чек.

    Проверяем, что предупреждение доезжает ДО КЛИЕНТА, а не остаётся
    в чистой функции: честный признак, которого никто не видит, — то же
    самое, что его отсутствие.
    """
    import json as _json

    payload = {
        "org_brand": "Ресторан",
        "amount": 3500,
        "datetime": "2024-12-26T15:15:00",
        "confidence": "high",
    }
    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    body = (await client.post("/api/receipts/ocr/", files=files)).json()

    assert body["date"] == "2024-12-26", "дата модели сохраняется как есть"
    assert any("26.12.2024" in w for w in body["warnings"]), (
        "неправдоподобная дата обязана попасть в warnings ответа"
    )


async def test_ocr_does_not_warn_about_normal_date(client, monkeypatch):
    # Ложная тревога учит не смотреть на предупреждения — проверяем обе стороны.
    import json as _json
    from datetime import date

    payload = {
        "org_brand": "Ресторан",
        "amount": 3500,
        "datetime": date.today().strftime("%Y-%m-%dT12:00:00"),
        "confidence": "high",
    }
    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    body = (await client.post("/api/receipts/ocr/", files=files)).json()
    assert body["warnings"] == []
