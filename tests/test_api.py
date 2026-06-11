"""API endpoint tests for AOCG AI Office.

Run against an in-memory fake pool (see conftest.py) — no real database is
touched. Each test gets a fresh store via the `db` / `seeded` fixtures.
"""

from datetime import date, datetime, timedelta

from app.categories_seed import seed_default_categories


# ─── GET /api/receipts/ ───────────────────────────────────────────────
async def test_get_receipts_returns_list(client):
    resp = await client.get("/api/receipts/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_get_receipts_with_data(client, seeded):
    resp = await client.get("/api/receipts/")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["org"] == "Лукойл"


# ─── POST /api/receipts/ ──────────────────────────────────────────────
async def test_create_receipt(client):
    payload = {"date": "2026-05-14", "org": "Магнит", "amount": 1234.56,
               "payment": "Наличные"}
    resp = await client.post("/api/receipts/", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] > 0
    assert body["org"] == "Магнит"
    assert body["amount"] == 1234.56
    # auto-categorization (вариант B) — резолв имени в category_id проверяется в
    # test_categorization_v2 с засеянным справочником; здесь орг не засеяна.


# ═══ Дедуп — 4 ветки (Фикс №3, 26.05). Жёсткий 409 только в ветках 0/1; ═══
# ═══ ветки 2/3 — мягкое предупреждение (чек создаётся, 200 + body.warning). ═══

# ─── Ветка 1 — точный дубль документа по паре (ФН, ФД) → 409 ─────────
async def test_create_receipt_duplicate_kkt_fn_returns_409(client):
    # Тот же документ (ФН+ФД) повторно → жёсткий 409. fd_num приходит из
    # raw_data (fiscalDocumentNumber), как у реального qr_scan.
    payload = {"date": "2026-05-14", "org": "Лукойл", "amount": 5000.0,
               "kkt_fn": "DUP-FN-123", "source": "qr_scan",
               "raw_data": {"fiscalDocumentNumber": "100500"}}
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200

    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["error"] == "duplicate_kkt_fn"
    assert detail["existing_id"] == first.json()["id"]


async def test_dedup_two_qr_same_fn_and_fd_blocks(client):
    # Тот же ФН И ТОТ ЖЕ ФД дважды → ветка 1 (точный дубль документа).
    payload = {"date": "2026-05-21", "org": "Лукойл", "amount": 3000.0,
               "kkt_fn": "QR-FN-555", "source": "qr_scan",
               "raw_data": {"fiscalDocumentNumber": "777"}}
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200
    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "duplicate_kkt_fn"
    assert second.json()["detail"]["existing_id"] == first.json()["id"]


async def test_dedup_same_fn_different_fd_both_pass(client):
    # БАГ Мере: один ФН на кассу, РАЗНЫЕ ФД = разные документы. Раньше второй
    # чек падал (ключ был ФН в одиночку) — теперь оба сохраняются.
    base = {"date": "2026-06-04", "org": 'ООО "Мере"', "amount": 2570.0,
            "source": "qr_scan", "kkt_fn": "7380440902249741"}
    first = await client.post("/api/receipts/", json={
        **base, "raw_data": {"fiscalDocumentNumber": "41946"}})
    assert first.status_code == 200
    second = await client.post("/api/receipts/", json={
        **base, "raw_data": {"fiscalDocumentNumber": "41947"}})
    assert second.status_code == 200
    assert second.json()["id"] != first.json()["id"]


async def test_dedup_fn_without_fd_no_hard_block(client):
    # ФН есть, ФД нет (raw_data без fiscalDocumentNumber) → жёсткая ветка 1 НЕ
    # срабатывает (пара неполна); чек создаётся (макс. мягкое предупреждение).
    payload = {"date": "2026-06-04", "org": 'ООО "Мере"', "amount": 2570.0,
               "source": "qr_scan", "kkt_fn": "7380440902249741",
               "raw_data": {"userInn": "7813679582"}}
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200
    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 200      # НЕ 409 — без ФД нет жёсткого дубля
    assert second.json()["id"] != first.json()["id"]


async def test_dedup_two_qr_with_different_fn_pass(client):
    # Q2-инвариант: два qr с РАЗНЫМИ fn = разные чеки (ФНС присвоила разные
    # номера). Динамический fn-фильтр в сильном composite их НЕ склеивает,
    # хотя дата+сумма+ИНН совпадают.
    base = {"date": "2026-05-21", "org": "Лукойл", "amount": 3000.0,
            "source": "qr_scan", "raw_data": {"user": "Лукойл", "userInn": "7707083893"}}
    first = await client.post("/api/receipts/", json={**base, "kkt_fn": "AAAA"})
    assert first.status_code == 200
    second = await client.post("/api/receipts/", json={**base, "kkt_fn": "BBBB"})
    assert second.status_code == 200
    assert second.json()["id"] != first.json()["id"]
    assert "warning" not in second.json()        # без ложного предупреждения


# ─── Ветка 0 — двойной тап (90 сек) для fn-less чеков → 409 ───────────
async def test_dedup_branch_0_double_tap_blocks(client):
    payload = {"date": "2026-05-21", "org": "Кафе Уют", "amount": 6400.0,
               "category": "Питание", "payment": "Наличные", "source": "manual"}
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200
    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["error"] == "double_tap_detected"
    assert detail["existing_id"] == first.json()["id"]


async def test_dedup_branch_0_photo_ocr_double_tap_blocks(client):
    # Реальный prod-дубль (id 39/41): два photo_ocr подряд, без надёжного fn.
    payload = {"date": "2026-05-21", "org": "Ресторан Мере", "amount": 1010.0,
               "category": "Питание", "payment": "Наличные", "source": "photo_ocr"}
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200
    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "double_tap_detected"


async def test_dedup_branch_0_after_90s_allows(client, db):
    # Тот же чек, но первый создан > 90 сек назад → не двойной тап; в окне
    # 7 дней без ИНН → слабое предупреждение, чек создаётся.
    old = datetime.utcnow() - timedelta(seconds=100)
    db.receipts.append(dict(id=1, date=date(2026, 5, 21), org="Кафе Уют",
                            category="Питание", payment="Наличные", amount=6400.0,
                            employee=None, fn=None, kkt_fn=None, raw_data=None,
                            source="manual", photo_url=None, org_id=1,
                            org_inn=None, created_at=old))
    db._rid = 1
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-21", "org": "Кафе Уют", "amount": 6400.0,
        "category": "Питание", "payment": "Наличные", "source": "manual"})
    assert resp.status_code == 200
    assert resp.json()["id"] != 1
    assert resp.json()["warning"]["confidence"] == "low"


# ─── Ветка 2 — сильное предупреждение (date+amount+ИНН), оба направления ──
async def test_dedup_strong_warning_photo_then_qr(client, db):
    # ГЛАВНЫЙ acceptance бага id3↔id4: photo_ocr создан первым (fn-less, ИНН в
    # колонке после Фикса №2), затем qr_scan того же чека → предупреждение, не
    # блок. Раньше qr_scan не видел photo_ocr-дубль (асимметрия C1).
    db.receipts.append(dict(id=1, date=date(2026, 5, 26), org='Ресторан "Мере"',
                            category="Питание", payment="Наличные", amount=1010.0,
                            employee=None, fn=None, kkt_fn=None, raw_data=None,
                            source="photo_ocr", photo_url=None, org_id=1,
                            org_inn="7813679582", created_at=datetime.utcnow()))
    db._rid = 1
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-26", "org": 'ООО "Мере"', "amount": 1010.0,
        "source": "qr_scan", "kkt_fn": "7380440902249741",
        "raw_data": {"user": 'ООО "Мере"', "userInn": "7813679582"}})
    assert resp.status_code == 200
    w = resp.json()["warning"]
    assert w["type"] == "possible_duplicate" and w["confidence"] == "high"
    assert w["similar_receipt_id"] == 1
    # Фаза A: similar_receipt отражает НАЙДЕННЫЙ чек id=1 (photo_ocr 'Ресторан "Мере"'),
    # не новый постящийся ('ООО "Мере"'). Фронт покажет эти поля в баннере.
    sr = w["similar_receipt"]
    assert sr["id"] == 1
    assert sr["org"] == 'Ресторан "Мере"'
    assert sr["amount"] == 1010.0 and isinstance(sr["amount"], float)
    assert sr["date"] == "2026-05-26"


async def test_dedup_strong_warning_qr_then_photo(client, db):
    # Обратное направление: qr_scan (с fn) создан первым, затем photo_ocr (fn-less)
    # того же чека. Динамический fn-фильтр позволяет fn-less чеку найти fn-ный дубль.
    db.receipts.append(dict(id=1, date=date(2026, 5, 26), org='ООО "Мере"',
                            category="Питание", payment="Наличные", amount=1010.0,
                            employee=None, fn="7380440902249741", kkt_fn="7380440902249741",
                            raw_data=None, source="qr_scan", photo_url=None, org_id=1,
                            org_inn="7813679582", created_at=datetime.utcnow()))
    db._rid = 1
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-26", "org": 'Ресторан "Мере"', "amount": 1010.0,
        "source": "photo_ocr",
        "raw_data": {"org_inn": "7813679582", "org_brand": 'Ресторан "Мере"', "items": []}})
    assert resp.status_code == 200
    w = resp.json()["warning"]
    assert w["confidence"] == "high"
    assert w["similar_receipt_id"] == 1
    sr = w["similar_receipt"]   # найденный чек id=1 — qr_scan 'ООО "Мере"'
    assert sr["id"] == 1 and sr["org"] == 'ООО "Мере"'
    assert sr["amount"] == 1010.0 and sr["date"] == "2026-05-26"


async def test_dedup_window_7_days_strong_warning(client, db):
    # Сильный ключ ловит дубль в окне 7 дней (создан 6 дней назад).
    db.receipts.append(dict(id=1, date=date(2026, 5, 26), org='ООО "Мере"',
                            category="Питание", payment="Наличные", amount=1010.0,
                            employee=None, fn=None, kkt_fn=None, raw_data=None,
                            source="photo_ocr", photo_url=None, org_id=1,
                            org_inn="7813679582",
                            created_at=datetime.utcnow() - timedelta(days=6)))
    db._rid = 1
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-26", "org": 'ООО "Мере"', "amount": 1010.0,
        "source": "qr_scan", "kkt_fn": "NEW-FN",
        "raw_data": {"user": 'ООО "Мере"', "userInn": "7813679582"}})
    assert resp.status_code == 200
    assert resp.json()["warning"]["confidence"] == "high"


async def test_dedup_outside_7_days_no_warning(client, db):
    # Старше 7 дней → вне окна, предупреждения нет.
    db.receipts.append(dict(id=1, date=date(2026, 5, 26), org='ООО "Мере"',
                            category="Питание", payment="Наличные", amount=1010.0,
                            employee=None, fn=None, kkt_fn=None, raw_data=None,
                            source="photo_ocr", photo_url=None, org_id=1,
                            org_inn="7813679582",
                            created_at=datetime.utcnow() - timedelta(days=8)))
    db._rid = 1
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-26", "org": 'ООО "Мере"', "amount": 1010.0,
        "source": "qr_scan", "kkt_fn": "NEW-FN",
        "raw_data": {"user": 'ООО "Мере"', "userInn": "7813679582"}})
    assert resp.status_code == 200
    assert "warning" not in resp.json()


# ─── Ветка 3 — слабое предупреждение (date+amount, без ИНН) ──────────
async def test_dedup_weak_warning_no_inn(client, db):
    old = datetime.utcnow() - timedelta(hours=2)   # вне 90 сек, в окне 7 дней
    db.receipts.append(dict(id=1, date=date(2026, 5, 21), org="Ларёк", category="Прочее",
                            payment="Наличные", amount=500.0, employee=None,
                            kkt_fn=None, raw_data=None, source="manual", photo_url=None,
                            org_id=1, org_inn=None, created_at=old))
    db._rid = 1
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-21", "org": "Ларёк", "amount": 500.0, "source": "manual"})
    assert resp.status_code == 200
    w = resp.json()["warning"]
    assert w["confidence"] == "low"
    assert w["similar_receipt_id"] == 1
    sr = w["similar_receipt"]   # найденный чек id=1 — manual "Ларёк"
    assert sr["id"] == 1 and sr["org"] == "Ларёк"
    assert sr["amount"] == 500.0 and sr["date"] == "2026-05-21"


async def test_dedup_invalid_inn_falls_to_weak(client, db):
    # Невалидный ИНН отфильтрован парсером ФНС (org_inn=None) → слабая ветка.
    db.receipts.append(dict(id=1, date=date(2026, 5, 21), org="Кафе", category="Питание",
                            payment="Наличные", amount=700.0, employee=None,
                            kkt_fn=None, raw_data=None, source="photo_ocr", photo_url=None,
                            org_id=1, org_inn=None, created_at=datetime.utcnow() - timedelta(hours=1)))
    db._rid = 1
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-21", "org": "Кафе", "amount": 700.0, "source": "qr_scan",
        "kkt_fn": "SOME-FN", "raw_data": {"user": "Кафе", "userInn": "1234567890"}})
    assert resp.status_code == 200
    assert resp.json()["org_inn"] is None              # парсер отбросил невалидный ИНН
    assert resp.json()["warning"]["confidence"] == "low"


# ─── C3: меняемые поля (category/payment) НЕ ломают дедуп ─────────────
async def test_dedup_category_and_payment_not_in_key(client, db):
    # У сохранённого чека category/payment отличаются от нового — предупреждение
    # всё равно срабатывает (в ключ входят только date+amount+ИНН).
    db.receipts.append(dict(id=1, date=date(2026, 5, 26), org='ООО "Мере"',
                            category="Прочее", payment="Корп.карта", amount=1010.0,
                            employee=None, fn="FN-1", kkt_fn="FN-1", raw_data=None,
                            source="qr_scan", photo_url=None, org_id=1,
                            org_inn="7813679582", created_at=datetime.utcnow()))
    db._rid = 1
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-26", "org": 'Ресторан "Мере"', "amount": 1010.0,
        "category": "Питание", "payment": "Наличные", "source": "photo_ocr",
        "raw_data": {"org_inn": "7813679582", "items": []}})
    assert resp.status_code == 200
    assert resp.json()["warning"]["confidence"] == "high"


async def test_dedup_patch_change_doesnt_break_dedup(client, db):
    # Вариант 3 из диагностики: пользователь меняет category через PATCH ПОСЛЕ
    # создания. Раньше это рассинхронизировало composite-ключ; теперь category
    # не в ключе, поэтому последующий дубль по date+amount+ИНН ловится.
    db.receipts.append(dict(id=1, date=date(2026, 5, 26), org='ООО "Мере"',
                            category="Не указано", payment="Наличные", amount=1010.0,
                            employee=None, fn=None, kkt_fn=None, raw_data=None,
                            source="photo_ocr", photo_url=None, org_id=1,
                            org_inn="7813679582", created_at=datetime.utcnow()))
    db._rid = 1
    patched = await client.patch("/api/receipts/1", json={"category": "Питание"})
    # вариант B: строки category в ответе нет, ручной выбор фиксируется category_manual
    assert patched.status_code == 200 and patched.json()["category_manual"] is True

    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-26", "org": 'ООО "Мере"', "amount": 1010.0,
        "source": "qr_scan", "kkt_fn": "NEW-FN",
        "raw_data": {"user": 'ООО "Мере"', "userInn": "7813679582"}})
    assert resp.status_code == 200
    w = resp.json()["warning"]
    assert w["similar_receipt_id"] == 1
    # category изменён через PATCH, но org похожего чека в баннере неизменен.
    assert w["similar_receipt"]["id"] == 1 and w["similar_receipt"]["org"] == 'ООО "Мере"'


# ─── Задача №9 фаза A — body.warning.similar_receipt (карточка для фронта) ──
async def test_warning_similar_receipt_includes_all_fields(client, db):
    # similar_receipt должен содержать {id, amount, org, date} в правильных
    # JSON-типах: id=int, org=str, amount=float, date=str ISO ("YYYY-MM-DD").
    db.receipts.append(dict(id=1, date=date(2026, 5, 26), org='ООО "Мере"',
                            category="Питание", payment="Наличные", amount=1010.0,
                            employee=None, fn=None, kkt_fn=None, raw_data=None,
                            source="photo_ocr", photo_url=None, org_id=1,
                            org_inn="7813679582", created_at=datetime.utcnow()))
    db._rid = 1
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-26", "org": 'ООО "Мере"', "amount": 1010.0,
        "source": "qr_scan", "kkt_fn": "FN-NEW",
        "raw_data": {"user": 'ООО "Мере"', "userInn": "7813679582"}})
    assert resp.status_code == 200
    sr = resp.json()["warning"]["similar_receipt"]
    assert set(sr) == {"id", "org", "amount", "date"}
    assert isinstance(sr["id"], int) and sr["id"] == 1
    assert isinstance(sr["org"], str) and sr["org"] == 'ООО "Мере"'
    assert isinstance(sr["amount"], float) and sr["amount"] == 1010.0
    assert isinstance(sr["date"], str) and sr["date"] == "2026-05-26"


async def test_warning_backward_compat_id_field(client, db):
    # similar_receipt_id (deprecated) сохраняется параллельно similar_receipt —
    # старый фронт, читающий только id, не ломается.
    db.receipts.append(dict(id=1, date=date(2026, 5, 21), org="Ларёк", category="Прочее",
                            payment="Наличные", amount=500.0, employee=None,
                            kkt_fn=None, raw_data=None, source="manual", photo_url=None,
                            org_id=1, org_inn=None,
                            created_at=datetime.utcnow() - timedelta(hours=2)))
    db._rid = 1
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-21", "org": "Ларёк", "amount": 500.0, "source": "manual"})
    assert resp.status_code == 200
    w = resp.json()["warning"]
    assert w["similar_receipt_id"] == 1                      # deprecated, но есть
    assert w["similar_receipt"]["id"] == w["similar_receipt_id"]   # согласованы


# ─── Задача №9 фаза C — warning.duplicates (массив всех дублей + новый) ──
def _seed_photo_dup(db, *, in_report=False):
    """Существующий photo_ocr-чек (fn-less, ИНН в колонке) за 5 мин до нового."""
    db.receipts.append(dict(id=1, date=date(2026, 5, 26), org='ООО "Мере"',
                            category="Питание", payment="Наличные", amount=1010.0,
                            employee=None, fn=None, kkt_fn=None, raw_data=None,
                            source="photo_ocr", photo_url=None, org_id=1,
                            org_inn="7813679582",
                            created_at=datetime.utcnow() - timedelta(minutes=5)))
    db._rid = 1
    if in_report:
        db.report_items.append({"report_id": 1, "receipt_id": 1})


async def _post_qr_dup(client):
    return await client.post("/api/receipts/", json={
        "date": "2026-05-26", "org": 'ООО "Мере"', "amount": 1010.0,
        "source": "qr_scan", "kkt_fn": "FN-NEW",
        "raw_data": {"user": 'ООО "Мере"', "userInn": "7813679582"}})


async def test_warning_duplicates_includes_array(client, db):
    _seed_photo_dup(db)
    resp = await _post_qr_dup(client)
    assert resp.status_code == 200
    dups = resp.json()["warning"]["duplicates"]
    assert isinstance(dups, list) and len(dups) == 2
    assert dups[0]["id"] == 1                      # created_at ASC: существующий первым
    assert set(dups[0]) == {"id", "org", "amount", "date", "source", "deletable", "in_report", "is_new"}


async def test_warning_duplicates_includes_new_receipt(client, db):
    _seed_photo_dup(db)
    resp = await _post_qr_dup(client)
    dups = resp.json()["warning"]["duplicates"]
    new = [d for d in dups if d["is_new"]]
    assert len(new) == 1 and new[0]["id"] == resp.json()["id"]
    assert new[0]["source"] == "qr_scan"
    assert sum(1 for d in dups if not d["is_new"]) == 1


async def test_warning_duplicates_marks_deletable(client, db):
    # photo_ocr (kkt_fn NULL) → deletable True; qr_scan (kkt_fn) → deletable False.
    _seed_photo_dup(db)
    resp = await _post_qr_dup(client)
    dups = {d["id"]: d for d in resp.json()["warning"]["duplicates"]}
    assert dups[1]["deletable"] is True
    assert dups[resp.json()["id"]]["deletable"] is False


async def test_warning_duplicates_marks_in_report(client, db):
    _seed_photo_dup(db, in_report=True)            # id=1 уже в отчёте
    resp = await _post_qr_dup(client)
    dups = {d["id"]: d for d in resp.json()["warning"]["duplicates"]}
    assert dups[1]["in_report"] is True
    assert dups[resp.json()["id"]]["in_report"] is False   # только что создан


# ─── (ФН, ФД) UniqueViolation guard: cross-org collision -> 409 ──────
async def test_unique_violation_kkt_fn_cross_org_returns_409(client, db):
    # SELECT-дедуп per-org (WHERE kkt_fn=$1 AND fd_num=$2 AND org_id=$3), а индекс
    # receipts_kkt_fn_fd_unique — ГЛОБАЛЬНЫЙ по паре (ФН, ФД). Тот же документ
    # (ФН+ФД) уже есть в другой org (org_id=2). Пост в org 1 промахивается мимо
    # per-org дедупа, доходит до INSERT, ловится глобальным индексом → 409.
    db.receipts.append(dict(id=99, date=date(2026, 5, 1), org="Чужая Орг",
                            category="Прочее", payment=None, amount=10.0, employee=None,
                            fn="GLOBAL-X", kkt_fn="GLOBAL-X", fd_num="555", raw_data=None,
                            source="qr_scan", photo_url=None, org_id=2,
                            created_at=datetime.utcnow()))

    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-22", "org": "Лукойл", "amount": 777.0,
        "kkt_fn": "GLOBAL-X", "source": "qr_scan",
        "raw_data": {"fiscalDocumentNumber": "555"}})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "duplicate_kkt_fn_cross_org"


async def test_photo_ocr_with_fn_not_written_to_columns(client):
    # Variant A: a photo_ocr receipt never writes its (unreliable) OCR number to
    # the kkt_fn column — it stays only in raw_data.fn for reference.
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-22", "org": "Кофейня", "amount": 250.0,
        "source": "photo_ocr", "kkt_fn": "OCR_HALLUCINATED_FN",
        "raw_data": {"fn": "OCR_HALLUCINATED_FN", "items": []}})
    assert resp.status_code == 200
    rid = resp.json()["id"]

    row = (await client.get(f"/api/receipts/{rid}")).json()
    assert row["kkt_fn"] is None
    assert row["raw_data"]["fn"] == "OCR_HALLUCINATED_FN"   # preserved for reference


# ─── qr_scan: FNS raw_data parsed into typed columns + receipt_items ──
async def test_qr_scan_parses_raw_data_into_columns_and_items(client, db):
    raw = {
        "user": 'ООО "Астер"', "userInn": "7707083893",
        "retailPlace": "Аптека №1", "retailPlaceAddress": "Москва, ул. Ленина, 1",
        "dateTime": "2026-05-20T13:42:00", "operationType": 1,
        "totalSum": 295500, "ecashTotalSum": 295500, "cashTotalSum": 0,
        "nds20": 49250, "appliedTaxationType": 2,
        "fiscalDriveNumber": "7380440700123456", "fiscalDocumentNumber": 1234,
        "fiscalSign": 987654321, "kktRegId": "0001234567012345", "operator": "Иванова И.И.",
        "items": [
            {"name": "Аспирин", "quantity": 2, "price": 100000, "sum": 200000, "nds": 1},
            {"name": "Бинт", "quantity": 1, "price": 95500, "sum": 95500, "nds": 1},
        ],
    }
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-20", "org": 'ООО "Астер"', "amount": 2955.0,
        "source": "qr_scan", "kkt_fn": "7380440700123456", "raw_data": raw})
    assert resp.status_code == 200
    body = resp.json()
    assert body["org_inn"] == "7707083893"          # valid INN preserved
    assert body["operation_type"] == "purchase"
    assert body["tax_system"] == "usn_income"
    assert body["org_brand"] == "Аптека №1"
    assert body["address"] == "Москва, ул. Ленина, 1"
    assert body["vat_20"] == 492.50
    assert body["kkt_rn"] == "0001234567012345"
    assert body["cashier"] == "Иванова И.И."
    assert body["payment_form"] == "card"
    assert body["kkt_fn"] == "7380440700123456"      # from dedup value, not parser

    items = [i for i in db.receipt_items if i["receipt_id"] == body["id"]]
    assert len(items) == 2
    assert items[0]["name"] == "Аспирин"
    assert items[0]["sum"] == 2000.0
    assert items[0]["vat_rate"] == "20"


# ─── photo_ocr: OCR raw_data parsed into typed columns + receipt_items ─
async def test_photo_ocr_parses_raw_data_into_columns_and_items(client, db):
    # Real prod OCR shape (id=3 family). Amounts in RUBLES, vat_rate a string,
    # datetime an ISO string, and an OCR-read fn that must be ignored (Вариант A).
    raw = {
        "org_legal": 'ООО "МЕРЕ"', "org_brand": 'Ресторан "Мере"',
        "org_inn": "7813679582", "address": "СПб, Ломейновольская, 7",
        "datetime": "2026-05-26T12:41:00", "currency": "RUB",
        "operation_type": "purchase", "payment_form": "card",
        "tax_system": "osno", "cashier": "Ботина Анастасия", "vat_20": 1110.00,
        "items": [
            {"position": 1, "name": "Эспрессо 40мл", "quantity": 1, "price": 250, "sum": 250, "vat_rate": "20"},
            {"position": 2, "name": "Зеленая греча", "quantity": 1, "price": 760, "sum": 760, "vat_rate": "10"},
        ],
        "fn": "OCR_HALLUCINATED_FN", "kkt_fn": "OCR_HALLUCINATED_FN",
    }
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-26", "org": 'Ресторан "Мере"', "amount": 1010.0,
        "source": "photo_ocr", "raw_data": raw})
    assert resp.status_code == 200
    body = resp.json()
    assert body["org_inn"] == "7813679582"           # OCR INN now lands in the column
    assert body["org_legal"] == 'ООО "МЕРЕ"'
    assert body["org_brand"] == 'Ресторан "Мере"'
    assert body["operation_type"] == "purchase"
    assert body["payment_form"] == "card"
    assert body["tax_system"] == "osno"
    assert body["cashier"] == "Ботина Анастасия"
    assert body["vat_20"] == 1110.00                  # rubles — not /100
    assert str(body["datetime"]).startswith("2026-05-26T12:41")
    assert body["kkt_fn"] is None                      # Вариант A — OCR fn never stored

    items = [i for i in db.receipt_items if i["receipt_id"] == body["id"]]
    assert len(items) == 2
    assert items[0]["name"] == "Эспрессо 40мл"
    assert items[0]["sum"] == 250.0                    # rubles
    assert items[0]["vat_rate"] == "20"                # string, not decoded


# ─── PATCH /api/receipts/{id} ─────────────────────────────────────────
async def test_patch_receipt_single_field(client, seeded):
    resp = await client.patch("/api/receipts/1", json={"payment": "Личная карта"})
    assert resp.status_code == 200
    assert resp.json()["payment"] == "Личная карта"
    assert resp.json()["org"] == "Лукойл"  # unchanged


async def test_patch_receipt_multiple_fields(client, seeded):
    resp = await client.patch("/api/receipts/1", json={
        "category": "Прочее", "org": "Газпром"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["category_manual"] is True   # ручной выбор категории (вариант B)
    assert body["org"] == "Газпром"


async def test_patch_receipt_no_fields_returns_existing(client, seeded):
    resp = await client.patch("/api/receipts/1", json={})
    assert resp.status_code == 200
    assert resp.json()["org"] == "Лукойл"


async def test_patch_receipt_not_found(client):
    resp = await client.patch("/api/receipts/999", json={"category": "X"})
    assert resp.status_code == 404


# ─── Смена категории чека: category_id резолвится + category_manual=TRUE ───
def _append_receipt(db, **over):
    base = dict(id=1, date=date(2026, 5, 20), org="Some Org", category="Не указано",
                payment="Наличные", amount=500.0, employee=None, fn=None, kkt_fn=None,
                raw_data=None, source="manual", photo_url=None, org_id=1,
                category_id=None, category_manual=False, created_at=datetime.utcnow())
    base.update(over)
    db.receipts.append(base)
    db._rid = base["id"]


async def test_patch_category_resolves_id_and_sets_manual(client, db):
    await seed_default_categories(db, 1)
    _append_receipt(db)
    resp = await client.patch("/api/receipts/1", json={"category": "Продукты для офиса"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["category_id"] == next(
        c["id"] for c in db.categories if c["org_id"] == 1 and c["name"] == "Продукты для офиса")
    assert body["category_manual"] is True


async def test_patch_category_unknown_name_falls_back_id(client, db):
    await seed_default_categories(db, 1)
    _append_receipt(db)
    resp = await client.patch("/api/receipts/1", json={"category": "Несуществующая"})
    body = resp.json()
    # строки category в ответе нет (вариант B); неизвестное имя → category_id фолбэк
    # «Прочие хозрасходы» (per-org), флаг ручного выбора всё равно TRUE
    assert body["category_id"] == next(
        c["id"] for c in db.categories if c["org_id"] == 1 and c["name"] == "Прочие хозрасходы")
    assert body["category_manual"] is True


async def test_patch_payment_keeps_category_manual_and_id(client, db):
    await seed_default_categories(db, 1)
    cid = next(c["id"] for c in db.categories if c["org_id"] == 1 and c["name"] == "Топливо")
    _append_receipt(db, category="Топливо", category_id=cid, category_manual=False)
    resp = await client.patch("/api/receipts/1", json={"payment": "Личная карта"})
    body = resp.json()
    assert body["payment"] == "Личная карта"
    assert body["category_manual"] is False   # не трогаем при смене payment
    assert body["category_id"] == cid          # category_id не изменился


# ─── DELETE /api/receipts/{id} ────────────────────────────────────────
async def test_delete_receipt(client):
    created = await client.post("/api/receipts/", json={
        "date": "2026-05-14", "org": "ВкусВилл", "amount": 800.0})
    rid = created.json()["id"]

    resp = await client.delete(f"/api/receipts/{rid}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    remaining = (await client.get("/api/receipts/")).json()
    assert all(r["id"] != rid for r in remaining)


async def test_delete_receipt_cross_org_ignored(client, db):
    """Юзер org A (client=org_id=1) не может удалить чек org B: ответ 200 {"ok": True}
    (anti-enumeration), но чужой чек остаётся нетронутым (закрытие IDOR P1)."""
    _mk(db, 99, source="manual", org_id=2)        # чужая орг
    resp = await client.delete("/api/receipts/99")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert any(r["id"] == 99 for r in db.receipts)   # чужой чек жив


async def test_delete_receipt_org_safe_report_items(client, db):
    """При одиночном cross-org DELETE связь report_items чужой орг НЕ трогается
    (аналог test_bulk_delete_org_safe_report_items)."""
    _mk(db, 99, source="manual", org_id=2)
    db.report_items.append({"report_id": 5, "receipt_id": 99})   # связь чужого чека
    resp = await client.delete("/api/receipts/99")
    assert resp.status_code == 200
    assert any(ri["receipt_id"] == 99 for ri in db.report_items)   # уцелела


# ─── POST /api/receipts/bulk-delete (задача №9 фаза C) ────────────────
def _mk(db, rid, *, source="manual", kkt_fn=None, org_id=1, amount=100.0):
    db.receipts.append(dict(id=rid, date=date(2026, 5, 20), org=f"Org{rid}",
                            category="Прочее", payment=None, amount=amount, employee=None,
                            kkt_fn=kkt_fn, raw_data=None, source=source,
                            photo_url=None, org_id=org_id, created_at=datetime.utcnow()))
    db._rid = max(db._rid, rid)


async def test_bulk_delete_basic(client, db):
    _mk(db, 1, source="manual")
    _mk(db, 2, source="photo_ocr")
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1, 2]})
    assert resp.status_code == 200
    body = resp.json()
    assert sorted(body["deleted"]) == [1, 2]
    assert body["blocked_fns"] == [] and body["blocked_in_report"] == []
    assert db.receipts == []


async def test_bulk_delete_cross_org_ignored(client, db):
    _mk(db, 1, source="manual", org_id=1)
    _mk(db, 99, source="manual", org_id=2)        # чужая орг
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1, 99]})
    body = resp.json()
    assert body["deleted"] == [1]
    assert 99 not in body["deleted"] + body["blocked_fns"] + body["blocked_in_report"]
    assert any(r["id"] == 99 for r in db.receipts)   # чужой чек жив


async def test_bulk_delete_blocks_in_report(client, db):
    # Чек в отчёте блокируется ВСЕГДА, даже с force=true.
    _mk(db, 1, source="qr_scan", kkt_fn="FN-1")
    db.report_items.append({"report_id": 1, "receipt_id": 1})
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1], "force": True})
    body = resp.json()
    assert body["blocked_in_report"] == [1]
    assert body["deleted"] == [] and body["blocked_fns"] == []
    assert any(r["id"] == 1 for r in db.receipts)


async def test_bulk_delete_blocks_fns_without_force(client, db):
    _mk(db, 1, source="qr_scan", kkt_fn="FN-1")
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1]})
    body = resp.json()
    assert body["blocked_fns"] == [1]
    assert body["deleted"] == []
    assert any(r["id"] == 1 for r in db.receipts)


async def test_bulk_delete_force_fns_succeeds(client, db):
    _mk(db, 1, source="qr_scan", kkt_fn="FN-1")
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1], "force": True})
    body = resp.json()
    assert body["deleted"] == [1] and body["blocked_fns"] == []
    assert db.receipts == []


async def test_bulk_delete_mixed_response(client, db):
    # 1 manual → удалить; 2 qr_scan без force → blocked_fns; 3 в отчёте → blocked_in_report.
    _mk(db, 1, source="manual")
    _mk(db, 2, source="qr_scan", kkt_fn="F2")
    _mk(db, 3, source="photo_ocr")
    db.report_items.append({"report_id": 1, "receipt_id": 3})
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1, 2, 3]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == [1]
    assert body["blocked_fns"] == [2]
    assert body["blocked_in_report"] == [3]
    assert {r["id"] for r in db.receipts} == {2, 3}


async def test_bulk_delete_org_safe_report_items(client, db):
    # Связь report_items чужого чека (org_id=2) НЕ трогается, даже если id передан.
    _mk(db, 1, source="manual", org_id=1)
    _mk(db, 99, source="manual", org_id=2)
    db.report_items.append({"report_id": 5, "receipt_id": 99})   # связь чужого чека
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1, 99]})
    assert resp.json()["deleted"] == [1]
    assert any(ri["receipt_id"] == 99 for ri in db.report_items)   # уцелела


# ─── GET /api/reports/ ────────────────────────────────────────────────
async def test_get_reports_returns_list(client):
    resp = await client.get("/api/reports/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ─── POST /api/reports/ ───────────────────────────────────────────────
async def test_create_report(client):
    rc = await client.post("/api/receipts/", json={
        "date": "2026-05-14", "org": "Лента", "amount": 999.0})
    rid = rc.json()["id"]

    resp = await client.post("/api/reports/", json={
        "title": "Майский отчёт", "total": 999.0, "receiptIds": [rid]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] > 0
    assert body["title"] == "Майский отчёт"
    assert body["receiptIds"] == [rid]


# ─── PATCH /api/reports/{id} ──────────────────────────────────────────
async def test_patch_report_status(client, seeded):
    resp = await client.patch("/api/reports/1", json={"status": "Отправлено"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "Отправлено"


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


# ─── GET /api/receipts/suggest-payment ────────────────────────────────
async def test_suggest_payment_returns_card(client, seeded):
    resp = await client.get("/api/receipts/suggest-payment", params={"org": "Лукойл"})
    assert resp.status_code == 200
    assert resp.json()["payment"] == "Корп.карта"


async def test_suggest_payment_returns_null_when_no_history(client):
    resp = await client.get("/api/receipts/suggest-payment", params={"org": "НеизвестнаяОрг"})
    assert resp.status_code == 200
    assert resp.json()["payment"] is None


# ─── POST /api/receipts/ocr/ ──────────────────────────────────────────
# A 1×1 PNG — anything we'd actually OCR is too big to inline, and the
# Anthropic client is mocked end-to-end so the image bytes never reach it.
import base64
import io

import pytest
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
        "org_legal": 'ООО "Тандер"', "org_brand": "Магнит", "org_inn": "7707083893",
        "address": "Москва", "datetime": "2026-05-15T13:42:00", "amount": 1234.56,
        "operation_type": "purchase", "payment_form": "card", "tax_system": "usn_income",
        "vat_20": 123.45,
        "items": [{"position": 1, "name": "Молоко", "quantity": 1, "price": 89.0,
                   "sum": 89.0, "vat_rate": "20"}],
        "confidence": "high",
    }
    import json as _json
    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["org_brand"] == "Магнит"
    assert body["org"] == "Магнит"            # alias: org_brand or org_legal
    assert body["amount"] == 1234.56
    # auto-categorization v2 picks up "Магнит" → "Продукты для офиса"
    assert body["category"] == "Продукты для офиса"


async def test_ocr_strips_markdown_fences(client, monkeypatch):
    """Claude sometimes wraps JSON in ```json ... ``` despite the prompt."""
    wrapped = '```json\n{"org_brand": "Лукойл", "amount": 3000, "confidence": "medium"}\n```'
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
        "org_legal": 'ООО "Денежные энергии"', "org_brand": "Aster",
        "org_inn": "7707083893", "address": "СПб, Невский 1",
        "datetime": "2026-05-21T12:17:00", "amount": 6660.0, "currency": "RUB",
        "operation_type": "purchase", "payment_form": "card",
        "payment_detail": "Корпоративная 3950", "card_last4": "3950",
        "tax_system": "usn_income", "vat_20": 1110.0, "vat_10": None, "vat_0": 5550.0,
        "cashier": "Дробушков Никита",
        "items": [{"position": 1, "name": "Шакшука", "quantity": 1.0,
                   "price": 750.0, "sum": 750.0, "vat_rate": "20"}],
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
    assert body["org"] == "Aster"               # org_brand or org_legal
    assert body["amount"] == 6660.0
    assert body["date"] == "2026-05-21"          # from datetime
    assert body["time"] == "12:17:00"
    assert body["payment_type"] == "card"        # from payment_form
    assert body["inn"] == "7707083893"           # alias of org_inn
    assert body["category"]                       # auto-categorized from org
    assert body["nds"] == 1110.0                  # vat_20 + vat_10(None)
    assert body["items"][0]["total"] == 750.0     # sum aliased to total


async def test_ocr_invalid_inn_returns_null(client, monkeypatch):
    """An OCR-misread INN with a bad checksum is dropped + a warning is added."""
    payload = {"org_brand": "Лавка", "amount": 100.0, "org_inn": "1234567890",
               "confidence": "high"}
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
        "21.05.2026 12:17":    "2026-05-21T12:17:00",
        "21.05.2026":          "2026-05-21T00:00:00",
        "2026-05-21":          "2026-05-21T00:00:00",
        "не дата":             None,
    }
    for raw, expected in cases.items():
        payload = {"org_brand": "X", "amount": 1.0, "datetime": raw, "confidence": "high"}
        _install_fake(monkeypatch, lambda kw, p=payload: _Response(_json.dumps(p)))
        files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
        body = (await client.post("/api/receipts/ocr/", files=files)).json()
        assert body["datetime"] == expected, f"{raw!r} → {body['datetime']!r}"


async def test_ocr_partial_response_fallback(client, monkeypatch):
    """No org / no amount → aliases are None, so the frontend shows 'partial'."""
    payload = {"address": "СПб", "confidence": "low"}   # neither org nor amount
    import json as _json
    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    body = (await client.post("/api/receipts/ocr/", files=files)).json()
    assert body["org"] is None        # frontend: !d.org → "partial"
    assert body["amount"] is None


async def test_ocr_no_fiscal_fields_requested(client, monkeypatch):
    """The prompt must NOT ask Claude for fiscal identifiers (OCR-unreliable)."""
    captured = {}

    def capture(kw):
        captured["prompt"] = kw["messages"][0]["content"][1]["text"]
        return _Response('{"org_brand": "X", "amount": 1, "confidence": "high"}')

    _install_fake(monkeypatch, capture)
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    await client.post("/api/receipts/ocr/", files=files)
    prompt = captured["prompt"]
    for key in ("kkt_fn", "kkt_rn", "kkt_serial", "fd_num", "fpd", "fiscalDriveNumber"):
        assert key not in prompt


# ─── POST /api/consent/ ───────────────────────────────────────────────
async def test_post_consent_records_row(client, db):
    resp = await client.post("/api/consent/", json={"user_id": "local_user"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] > 0
    assert body["policy_version"] == "1.0"
    assert body["consent_at"] is not None
    # row landed in the store with the frozen text
    assert len(db.consents) == 1
    assert db.consents[0]["user_id"] == "local_user"
    assert "Шукалович" in db.consents[0]["consent_text"]


async def test_post_consent_with_ip(client, db):
    resp = await client.post("/api/consent/", json={"user_id": "u1", "ip_address": "203.0.113.4"})
    assert resp.status_code == 200
    assert db.consents[0]["ip_address"] == "203.0.113.4"


async def test_post_consent_appends_on_reagree(client, db):
    """Re-agreement is intentional — we append rather than upsert."""
    await client.post("/api/consent/", json={"user_id": "u1"})
    await client.post("/api/consent/", json={"user_id": "u1"})
    assert len(db.consents) == 2


# ─── GET /api/consent/{user_id} ───────────────────────────────────────
async def test_get_consent_returns_null_when_none(client):
    resp = await client.get("/api/consent/never_consented")
    assert resp.status_code == 200
    assert resp.json() is None


async def test_get_consent_returns_latest(client, db):
    await client.post("/api/consent/", json={"user_id": "u1"})
    second = await client.post("/api/consent/", json={"user_id": "u1"})
    resp = await client.get("/api/consent/u1")
    assert resp.status_code == 200
    body = resp.json()
    # 'latest' = highest id, which the POST returned
    assert body["id"] == second.json()["id"]
    assert body["policy_version"] == "1.0"


async def test_get_consent_isolates_users(client, db):
    await client.post("/api/consent/", json={"user_id": "alice"})
    resp = await client.get("/api/consent/bob")
    assert resp.status_code == 200
    assert resp.json() is None


# ─── POST /api/receipts/  source + photo_url ──────────────────────────
async def test_create_receipt_defaults_source_to_manual(client):
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0}
    body = (await client.post("/api/receipts/", json=payload)).json()
    assert body["source"] == "manual"
    assert body["photo_url"] is None


async def test_create_receipt_honors_explicit_source(client):
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0,
               "source": "qr_scan"}
    body = (await client.post("/api/receipts/", json=payload)).json()
    assert body["source"] == "qr_scan"


async def test_create_receipt_persists_photo_url(client):
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0,
               "source": "photo_ocr", "photo_url": "https://r2.example/abc.jpg"}
    body = (await client.post("/api/receipts/", json=payload)).json()
    assert body["source"] == "photo_ocr"
    assert body["photo_url"] == "https://r2.example/abc.jpg"


async def test_get_receipts_returns_source_field(client, seeded):
    body = (await client.get("/api/receipts/")).json()
    assert "source" in body[0]
    assert body[0]["source"] == "manual"  # seeded receipt defaults


# ─── GET /api/receipts/{id}/photo ─────────────────────────────────────
import base64 as _b64

# A minimal 1×1 PNG so the byte-equality assertion is meaningful.
_PNG_1x1_BYTES = _b64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


async def test_get_photo_404_when_receipt_missing(client):
    resp = await client.get("/api/receipts/9999/photo")
    assert resp.status_code == 404


async def test_get_photo_404_when_no_photo(client):
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0}
    created = (await client.post("/api/receipts/", json=payload)).json()
    resp = await client.get(f"/api/receipts/{created['id']}/photo")
    assert resp.status_code == 404


async def test_get_photo_returns_inline_bytes_from_base64(client):
    photo_b64 = _b64.b64encode(_PNG_1x1_BYTES).decode("ascii")
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0,
               "source": "photo_ocr",
               "raw_data": {"photo_base64": photo_b64, "items": []}}
    created = (await client.post("/api/receipts/", json=payload)).json()
    resp = await client.get(f"/api/receipts/{created['id']}/photo")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")
    assert resp.content == _PNG_1x1_BYTES


async def test_get_photo_redirects_when_photo_url_set(client):
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0,
               "source": "photo_ocr", "photo_url": "https://r2.example/abc.jpg"}
    created = (await client.post("/api/receipts/", json=payload)).json()
    resp = await client.get(f"/api/receipts/{created['id']}/photo",
                            follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://r2.example/abc.jpg"


async def test_get_photo_prefers_url_over_base64(client):
    """When both are present the external URL wins — R2 supersedes inline."""
    photo_b64 = _b64.b64encode(_PNG_1x1_BYTES).decode("ascii")
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0,
               "source": "photo_ocr",
               "photo_url": "https://r2.example/abc.jpg",
               "raw_data": {"photo_base64": photo_b64}}
    created = (await client.post("/api/receipts/", json=payload)).json()
    resp = await client.get(f"/api/receipts/{created['id']}/photo",
                            follow_redirects=False)
    assert resp.status_code == 302


# ─── S-15: IDOR при создании отчёта — receiptIds скоупятся по org_id ───
async def test_create_report_own_receipts_ok(client, db):
    # Свои чеки (org_id=1) → 200, report_items записаны.
    now = datetime.utcnow()
    db.receipts.append(dict(id=10, date=date(2026, 6, 1), org="X", amount=100.0,
                            org_id=1, created_at=now))
    db.receipts.append(dict(id=11, date=date(2026, 6, 1), org="Y", amount=200.0,
                            org_id=1, created_at=now))
    resp = await client.post("/api/reports/", json={
        "title": "Июнь", "total": 300.0, "receiptIds": [10, 11]})
    assert resp.status_code == 200
    assert set(resp.json()["receiptIds"]) == {10, 11}
    assert {ri["receipt_id"] for ri in db.report_items} == {10, 11}
    assert len(db.reports) == 1


async def test_create_report_foreign_receipt_403_nothing_written(client, db):
    # Чужой чек (org_id=2) в списке → 403, и отчёт, и позиции откатаны.
    now = datetime.utcnow()
    db.receipts.append(dict(id=10, date=date(2026, 6, 1), org="X", amount=100.0,
                            org_id=1, created_at=now))
    db.receipts.append(dict(id=20, date=date(2026, 6, 1), org="Чужая", amount=50.0,
                            org_id=2, created_at=now))
    resp = await client.post("/api/reports/", json={
        "title": "Атака", "total": 150.0, "receiptIds": [10, 20]})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Один или несколько чеков недоступны"
    assert db.reports == []        # откат: отчёт не появился
    assert db.report_items == []   # откат: позиции не появились


async def test_create_report_nonexistent_receipt_403(client, db):
    # Несуществующий id 999999 ловится так же (не только чужие, но и фейковые).
    now = datetime.utcnow()
    db.receipts.append(dict(id=10, date=date(2026, 6, 1), org="X", amount=100.0,
                            org_id=1, created_at=now))
    resp = await client.post("/api/reports/", json={
        "title": "Фейк", "total": 100.0, "receiptIds": [10, 999999]})
    assert resp.status_code == 403
    assert db.reports == []
    assert db.report_items == []
