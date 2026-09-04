"""API endpoint tests for AOCG AI Office.

Run against an in-memory fake pool (see conftest.py) — no real database is
touched. Each test gets a fresh store via the `db` / `seeded` fixtures.
"""

from datetime import date, datetime, timedelta


from app.categories_seed import seed_default_categories
from app.routers.consent import POLICY_VERSION


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
    payload = {
        "date": "2026-05-14",
        "org": "Магнит",
        "amount": 1234.56,
        "payment": "Наличные",
    }
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
    payload = {
        "date": "2026-05-14",
        "org": "Лукойл",
        "amount": 5000.0,
        "kkt_fn": "DUP-FN-123",
        "source": "qr_scan",
        "raw_data": {"fiscalDocumentNumber": "100500"},
    }
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200

    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["error"] == "duplicate_kkt_fn"
    assert detail["existing_id"] == first.json()["id"]


async def test_dedup_two_qr_same_fn_and_fd_blocks(client):
    # Тот же ФН И ТОТ ЖЕ ФД дважды → ветка 1 (точный дубль документа).
    payload = {
        "date": "2026-05-21",
        "org": "Лукойл",
        "amount": 3000.0,
        "kkt_fn": "QR-FN-555",
        "source": "qr_scan",
        "raw_data": {"fiscalDocumentNumber": "777"},
    }
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200
    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "duplicate_kkt_fn"
    assert second.json()["detail"]["existing_id"] == first.json()["id"]


async def test_dedup_same_fn_different_fd_both_pass(client):
    # БАГ Мере: один ФН на кассу, РАЗНЫЕ ФД = разные документы. Раньше второй
    # чек падал (ключ был ФН в одиночку) — теперь оба сохраняются.
    base = {
        "date": "2026-06-04",
        "org": 'ООО "Мере"',
        "amount": 2570.0,
        "source": "qr_scan",
        "kkt_fn": "7380440902249741",
    }
    first = await client.post(
        "/api/receipts/", json={**base, "raw_data": {"fiscalDocumentNumber": "41946"}}
    )
    assert first.status_code == 200
    second = await client.post(
        "/api/receipts/", json={**base, "raw_data": {"fiscalDocumentNumber": "41947"}}
    )
    assert second.status_code == 200
    assert second.json()["id"] != first.json()["id"]


async def test_dedup_fn_without_fd_no_hard_block(client):
    # ФН есть, ФД нет (raw_data без fiscalDocumentNumber) → жёсткая ветка 1 НЕ
    # срабатывает (пара неполна); чек создаётся (макс. мягкое предупреждение).
    payload = {
        "date": "2026-06-04",
        "org": 'ООО "Мере"',
        "amount": 2570.0,
        "source": "qr_scan",
        "kkt_fn": "7380440902249741",
        "raw_data": {"userInn": "7813679582"},
    }
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200
    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 200  # НЕ 409 — без ФД нет жёсткого дубля
    assert second.json()["id"] != first.json()["id"]


async def test_dedup_two_qr_with_different_fn_pass(client):
    # Q2-инвариант: два qr с РАЗНЫМИ fn = разные чеки (ФНС присвоила разные
    # номера). Динамический fn-фильтр в сильном composite их НЕ склеивает,
    # хотя дата+сумма+ИНН совпадают.
    base = {
        "date": "2026-05-21",
        "org": "Лукойл",
        "amount": 3000.0,
        "source": "qr_scan",
        "raw_data": {"user": "Лукойл", "userInn": "7707083893"},
    }
    first = await client.post("/api/receipts/", json={**base, "kkt_fn": "AAAA"})
    assert first.status_code == 200
    second = await client.post("/api/receipts/", json={**base, "kkt_fn": "BBBB"})
    assert second.status_code == 200
    assert second.json()["id"] != first.json()["id"]
    assert "warning" not in second.json()  # без ложного предупреждения


# ─── Ветка 0 — двойной тап (90 сек) для fn-less чеков → 409 ───────────
async def test_dedup_branch_0_double_tap_blocks(client):
    payload = {
        "date": "2026-05-21",
        "org": "Кафе Уют",
        "amount": 6400.0,
        "category": "Питание",
        "payment": "Наличные",
        "source": "manual",
    }
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200
    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["error"] == "double_tap_detected"
    assert detail["existing_id"] == first.json()["id"]


async def test_dedup_branch_0_photo_ocr_double_tap_blocks(client):
    # Реальный prod-дубль (id 39/41): два photo_ocr подряд, без надёжного fn.
    payload = {
        "date": "2026-05-21",
        "org": "Ресторан Мере",
        "amount": 1010.0,
        "category": "Питание",
        "payment": "Наличные",
        "source": "photo_ocr",
    }
    first = await client.post("/api/receipts/", json=payload)
    assert first.status_code == 200
    second = await client.post("/api/receipts/", json=payload)
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "double_tap_detected"


async def test_dedup_branch_0_after_90s_allows(client, db):
    # Тот же чек, но первый создан > 90 сек назад → не двойной тап; в окне
    # 7 дней без ИНН → слабое предупреждение, чек создаётся.
    old = datetime.utcnow() - timedelta(seconds=100)
    db.receipts.append(
        dict(
            id=1,
            date=date(2026, 5, 21),
            org="Кафе Уют",
            category="Питание",
            payment="Наличные",
            amount=6400.0,
            employee=None,
            fn=None,
            kkt_fn=None,
            raw_data=None,
            source="manual",
            photo_url=None,
            org_id=1,
            org_inn=None,
            created_at=old,
        )
    )
    db._rid = 1
    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-21",
            "org": "Кафе Уют",
            "amount": 6400.0,
            "category": "Питание",
            "payment": "Наличные",
            "source": "manual",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["id"] != 1
    assert resp.json()["warning"]["confidence"] == "low"


# ─── Ветка 2 — сильное предупреждение (date+amount+ИНН), оба направления ──
async def test_dedup_strong_warning_photo_then_qr(client, db):
    # ГЛАВНЫЙ acceptance бага id3↔id4: photo_ocr создан первым (fn-less, ИНН в
    # колонке после Фикса №2), затем qr_scan того же чека → предупреждение, не
    # блок. Раньше qr_scan не видел photo_ocr-дубль (асимметрия C1).
    db.receipts.append(
        dict(
            id=1,
            date=date(2026, 5, 26),
            org='Ресторан "Мере"',
            category="Питание",
            payment="Наличные",
            amount=1010.0,
            employee=None,
            fn=None,
            kkt_fn=None,
            raw_data=None,
            source="photo_ocr",
            photo_url=None,
            org_id=1,
            org_inn="7813679582",
            created_at=datetime.utcnow(),
        )
    )
    db._rid = 1
    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-26",
            "org": 'ООО "Мере"',
            "amount": 1010.0,
            "source": "qr_scan",
            "kkt_fn": "7380440902249741",
            "raw_data": {"user": 'ООО "Мере"', "userInn": "7813679582"},
        },
    )
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
    db.receipts.append(
        dict(
            id=1,
            date=date(2026, 5, 26),
            org='ООО "Мере"',
            category="Питание",
            payment="Наличные",
            amount=1010.0,
            employee=None,
            fn="7380440902249741",
            kkt_fn="7380440902249741",
            raw_data=None,
            source="qr_scan",
            photo_url=None,
            org_id=1,
            org_inn="7813679582",
            created_at=datetime.utcnow(),
        )
    )
    db._rid = 1
    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-26",
            "org": 'Ресторан "Мере"',
            "amount": 1010.0,
            "source": "photo_ocr",
            "raw_data": {
                "org_inn": "7813679582",
                "org_brand": 'Ресторан "Мере"',
                "items": [],
            },
        },
    )
    assert resp.status_code == 200
    w = resp.json()["warning"]
    assert w["confidence"] == "high"
    assert w["similar_receipt_id"] == 1
    sr = w["similar_receipt"]  # найденный чек id=1 — qr_scan 'ООО "Мере"'
    assert sr["id"] == 1 and sr["org"] == 'ООО "Мере"'
    assert sr["amount"] == 1010.0 and sr["date"] == "2026-05-26"


async def test_dedup_window_7_days_strong_warning(client, db):
    # Сильный ключ ловит дубль в окне 7 дней (создан 6 дней назад).
    db.receipts.append(
        dict(
            id=1,
            date=date(2026, 5, 26),
            org='ООО "Мере"',
            category="Питание",
            payment="Наличные",
            amount=1010.0,
            employee=None,
            fn=None,
            kkt_fn=None,
            raw_data=None,
            source="photo_ocr",
            photo_url=None,
            org_id=1,
            org_inn="7813679582",
            created_at=datetime.utcnow() - timedelta(days=6),
        )
    )
    db._rid = 1
    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-26",
            "org": 'ООО "Мере"',
            "amount": 1010.0,
            "source": "qr_scan",
            "kkt_fn": "NEW-FN",
            "raw_data": {"user": 'ООО "Мере"', "userInn": "7813679582"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["warning"]["confidence"] == "high"


async def test_dedup_outside_7_days_no_warning(client, db):
    # Старше 7 дней → вне окна, предупреждения нет.
    db.receipts.append(
        dict(
            id=1,
            date=date(2026, 5, 26),
            org='ООО "Мере"',
            category="Питание",
            payment="Наличные",
            amount=1010.0,
            employee=None,
            fn=None,
            kkt_fn=None,
            raw_data=None,
            source="photo_ocr",
            photo_url=None,
            org_id=1,
            org_inn="7813679582",
            created_at=datetime.utcnow() - timedelta(days=8),
        )
    )
    db._rid = 1
    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-26",
            "org": 'ООО "Мере"',
            "amount": 1010.0,
            "source": "qr_scan",
            "kkt_fn": "NEW-FN",
            "raw_data": {"user": 'ООО "Мере"', "userInn": "7813679582"},
        },
    )
    assert resp.status_code == 200
    assert "warning" not in resp.json()


# ─── Ветка 3 — слабое предупреждение (date+amount, без ИНН) ──────────
async def test_dedup_weak_warning_no_inn(client, db):
    old = datetime.utcnow() - timedelta(hours=2)  # вне 90 сек, в окне 7 дней
    db.receipts.append(
        dict(
            id=1,
            date=date(2026, 5, 21),
            org="Ларёк",
            category="Прочее",
            payment="Наличные",
            amount=500.0,
            employee=None,
            kkt_fn=None,
            raw_data=None,
            source="manual",
            photo_url=None,
            org_id=1,
            org_inn=None,
            created_at=old,
        )
    )
    db._rid = 1
    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-21",
            "org": "Ларёк",
            "amount": 500.0,
            "source": "manual",
        },
    )
    assert resp.status_code == 200
    w = resp.json()["warning"]
    assert w["confidence"] == "low"
    assert w["similar_receipt_id"] == 1
    sr = w["similar_receipt"]  # найденный чек id=1 — manual "Ларёк"
    assert sr["id"] == 1 and sr["org"] == "Ларёк"
    assert sr["amount"] == 500.0 and sr["date"] == "2026-05-21"


async def test_dedup_invalid_inn_falls_to_weak(client, db):
    # Невалидный ИНН отфильтрован парсером ФНС (org_inn=None) → слабая ветка.
    db.receipts.append(
        dict(
            id=1,
            date=date(2026, 5, 21),
            org="Кафе",
            category="Питание",
            payment="Наличные",
            amount=700.0,
            employee=None,
            kkt_fn=None,
            raw_data=None,
            source="photo_ocr",
            photo_url=None,
            org_id=1,
            org_inn=None,
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
    )
    db._rid = 1
    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-21",
            "org": "Кафе",
            "amount": 700.0,
            "source": "qr_scan",
            "kkt_fn": "SOME-FN",
            "raw_data": {"user": "Кафе", "userInn": "1234567890"},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["org_inn"] is None  # парсер отбросил невалидный ИНН
    assert resp.json()["warning"]["confidence"] == "low"


# ─── C3: меняемые поля (category/payment) НЕ ломают дедуп ─────────────
async def test_dedup_category_and_payment_not_in_key(client, db):
    # У сохранённого чека category/payment отличаются от нового — предупреждение
    # всё равно срабатывает (в ключ входят только date+amount+ИНН).
    db.receipts.append(
        dict(
            id=1,
            date=date(2026, 5, 26),
            org='ООО "Мере"',
            category="Прочее",
            payment="Корп.карта",
            amount=1010.0,
            employee=None,
            fn="FN-1",
            kkt_fn="FN-1",
            raw_data=None,
            source="qr_scan",
            photo_url=None,
            org_id=1,
            org_inn="7813679582",
            created_at=datetime.utcnow(),
        )
    )
    db._rid = 1
    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-26",
            "org": 'Ресторан "Мере"',
            "amount": 1010.0,
            "category": "Питание",
            "payment": "Наличные",
            "source": "photo_ocr",
            "raw_data": {"org_inn": "7813679582", "items": []},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["warning"]["confidence"] == "high"


async def test_dedup_patch_change_doesnt_break_dedup(client, db):
    # Вариант 3 из диагностики: пользователь меняет category через PATCH ПОСЛЕ
    # создания. Раньше это рассинхронизировало composite-ключ; теперь category
    # не в ключе, поэтому последующий дубль по date+amount+ИНН ловится.
    db.receipts.append(
        dict(
            id=1,
            date=date(2026, 5, 26),
            org='ООО "Мере"',
            category="Не указано",
            payment="Наличные",
            amount=1010.0,
            employee=None,
            fn=None,
            kkt_fn=None,
            raw_data=None,
            source="photo_ocr",
            photo_url=None,
            org_id=1,
            org_inn="7813679582",
            created_at=datetime.utcnow(),
        )
    )
    db._rid = 1
    patched = await client.patch("/api/receipts/1", json={"category": "Питание"})
    # вариант B: строки category в ответе нет, ручной выбор фиксируется category_manual
    assert patched.status_code == 200 and patched.json()["category_manual"] is True

    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-26",
            "org": 'ООО "Мере"',
            "amount": 1010.0,
            "source": "qr_scan",
            "kkt_fn": "NEW-FN",
            "raw_data": {"user": 'ООО "Мере"', "userInn": "7813679582"},
        },
    )
    assert resp.status_code == 200
    w = resp.json()["warning"]
    assert w["similar_receipt_id"] == 1
    # category изменён через PATCH, но org похожего чека в баннере неизменен.
    assert (
        w["similar_receipt"]["id"] == 1 and w["similar_receipt"]["org"] == 'ООО "Мере"'
    )


# ─── Задача №9 фаза A — body.warning.similar_receipt (карточка для фронта) ──
async def test_warning_similar_receipt_includes_all_fields(client, db):
    # similar_receipt должен содержать {id, amount, org, date} в правильных
    # JSON-типах: id=int, org=str, amount=float, date=str ISO ("YYYY-MM-DD").
    db.receipts.append(
        dict(
            id=1,
            date=date(2026, 5, 26),
            org='ООО "Мере"',
            category="Питание",
            payment="Наличные",
            amount=1010.0,
            employee=None,
            fn=None,
            kkt_fn=None,
            raw_data=None,
            source="photo_ocr",
            photo_url=None,
            org_id=1,
            org_inn="7813679582",
            created_at=datetime.utcnow(),
        )
    )
    db._rid = 1
    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-26",
            "org": 'ООО "Мере"',
            "amount": 1010.0,
            "source": "qr_scan",
            "kkt_fn": "FN-NEW",
            "raw_data": {"user": 'ООО "Мере"', "userInn": "7813679582"},
        },
    )
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
    db.receipts.append(
        dict(
            id=1,
            date=date(2026, 5, 21),
            org="Ларёк",
            category="Прочее",
            payment="Наличные",
            amount=500.0,
            employee=None,
            kkt_fn=None,
            raw_data=None,
            source="manual",
            photo_url=None,
            org_id=1,
            org_inn=None,
            created_at=datetime.utcnow() - timedelta(hours=2),
        )
    )
    db._rid = 1
    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-21",
            "org": "Ларёк",
            "amount": 500.0,
            "source": "manual",
        },
    )
    assert resp.status_code == 200
    w = resp.json()["warning"]
    assert w["similar_receipt_id"] == 1  # deprecated, но есть
    assert w["similar_receipt"]["id"] == w["similar_receipt_id"]  # согласованы


# ─── Задача №9 фаза C — warning.duplicates (массив всех дублей + новый) ──
def _seed_photo_dup(db, *, in_report=False):
    """Существующий photo_ocr-чек (fn-less, ИНН в колонке) за 5 мин до нового."""
    db.receipts.append(
        dict(
            id=1,
            date=date(2026, 5, 26),
            org='ООО "Мере"',
            category="Питание",
            payment="Наличные",
            amount=1010.0,
            employee=None,
            fn=None,
            kkt_fn=None,
            raw_data=None,
            source="photo_ocr",
            photo_url=None,
            org_id=1,
            org_inn="7813679582",
            created_at=datetime.utcnow() - timedelta(minutes=5),
        )
    )
    db._rid = 1
    if in_report:
        db.report_items.append({"report_id": 1, "receipt_id": 1})


async def _post_qr_dup(client):
    return await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-26",
            "org": 'ООО "Мере"',
            "amount": 1010.0,
            "source": "qr_scan",
            "kkt_fn": "FN-NEW",
            "raw_data": {"user": 'ООО "Мере"', "userInn": "7813679582"},
        },
    )


async def test_warning_duplicates_includes_array(client, db):
    _seed_photo_dup(db)
    resp = await _post_qr_dup(client)
    assert resp.status_code == 200
    dups = resp.json()["warning"]["duplicates"]
    assert isinstance(dups, list) and len(dups) == 2
    assert dups[0]["id"] == 1  # created_at ASC: существующий первым
    assert set(dups[0]) == {
        "id",
        "org",
        "amount",
        "date",
        "source",
        "deletable",
        "in_report",
        "is_new",
    }


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
    _seed_photo_dup(db, in_report=True)  # id=1 уже в отчёте
    resp = await _post_qr_dup(client)
    dups = {d["id"]: d for d in resp.json()["warning"]["duplicates"]}
    assert dups[1]["in_report"] is True
    assert dups[resp.json()["id"]]["in_report"] is False  # только что создан


# ─── (ФН, ФД) UniqueViolation guard: cross-org collision -> 409 ──────
async def test_unique_violation_kkt_fn_cross_org_returns_409(client, db):
    # SELECT-дедуп per-org (WHERE kkt_fn=$1 AND fd_num=$2 AND org_id=$3), а индекс
    # receipts_kkt_fn_fd_unique — ГЛОБАЛЬНЫЙ по паре (ФН, ФД). Тот же документ
    # (ФН+ФД) уже есть в другой org (org_id=2). Пост в org 1 промахивается мимо
    # per-org дедупа, доходит до INSERT, ловится глобальным индексом → 409.
    db.receipts.append(
        dict(
            id=99,
            date=date(2026, 5, 1),
            org="Чужая Орг",
            category="Прочее",
            payment=None,
            amount=10.0,
            employee=None,
            fn="GLOBAL-X",
            kkt_fn="GLOBAL-X",
            fd_num="555",
            raw_data=None,
            source="qr_scan",
            photo_url=None,
            org_id=2,
            created_at=datetime.utcnow(),
        )
    )

    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-22",
            "org": "Лукойл",
            "amount": 777.0,
            "kkt_fn": "GLOBAL-X",
            "source": "qr_scan",
            "raw_data": {"fiscalDocumentNumber": "555"},
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "duplicate_kkt_fn_cross_org"


async def test_photo_ocr_with_fn_not_written_to_columns(client):
    # Variant A: a photo_ocr receipt never writes its (unreliable) OCR number to
    # the kkt_fn column — it stays only in raw_data.fn for reference.
    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-22",
            "org": "Кофейня",
            "amount": 250.0,
            "source": "photo_ocr",
            "kkt_fn": "OCR_HALLUCINATED_FN",
            "raw_data": {"fn": "OCR_HALLUCINATED_FN", "items": []},
        },
    )
    assert resp.status_code == 200
    rid = resp.json()["id"]

    row = (await client.get(f"/api/receipts/{rid}")).json()
    assert row["kkt_fn"] is None
    assert row["raw_data"]["fn"] == "OCR_HALLUCINATED_FN"  # preserved for reference


# ─── qr_scan: FNS raw_data parsed into typed columns + receipt_items ──
async def test_qr_scan_parses_raw_data_into_columns_and_items(client, db):
    raw = {
        "user": 'ООО "Астер"',
        "userInn": "7707083893",
        "retailPlace": "Аптека №1",
        "retailPlaceAddress": "Москва, ул. Ленина, 1",
        "dateTime": "2026-05-20T13:42:00",
        "operationType": 1,
        "totalSum": 295500,
        "ecashTotalSum": 295500,
        "cashTotalSum": 0,
        "nds20": 49250,
        "appliedTaxationType": 2,
        "fiscalDriveNumber": "7380440700123456",
        "fiscalDocumentNumber": 1234,
        "fiscalSign": 987654321,
        "kktRegId": "0001234567012345",
        "operator": "Иванова И.И.",
        "items": [
            {
                "name": "Аспирин",
                "quantity": 2,
                "price": 100000,
                "sum": 200000,
                "nds": 1,
            },
            {"name": "Бинт", "quantity": 1, "price": 95500, "sum": 95500, "nds": 1},
        ],
    }
    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-20",
            "org": 'ООО "Астер"',
            "amount": 2955.0,
            "source": "qr_scan",
            "kkt_fn": "7380440700123456",
            "raw_data": raw,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["org_inn"] == "7707083893"  # valid INN preserved
    assert body["operation_type"] == "purchase"
    assert body["tax_system"] == "usn_income"
    assert body["org_brand"] == "Аптека №1"
    assert body["address"] == "Москва, ул. Ленина, 1"
    # NDS-CLEANUP ②: у чека ФНС НДС живёт в разбивке, отдельных колонок ставок нет
    assert body["vat_breakdown"] == {"20": 492.50}
    assert body["vat_total"] is None  # ФНС: сумма не нужна, есть разбивка
    assert body["kkt_rn"] == "0001234567012345"
    assert body["cashier"] == "Иванова И.И."
    assert body["payment_form"] == "card"
    assert body["kkt_fn"] == "7380440700123456"  # from dedup value, not parser

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
        "org_legal": 'ООО "МЕРЕ"',
        "org_brand": 'Ресторан "Мере"',
        "org_inn": "7813679582",
        "address": "СПб, Ломейновольская, 7",
        "datetime": "2026-05-26T12:41:00",
        "currency": "RUB",
        "operation_type": "purchase",
        "payment_form": "card",
        "tax_system": "osno",
        "cashier": "Ботина Анастасия",
        "vat_20": 1110.00,
        "items": [
            {
                "position": 1,
                "name": "Эспрессо 40мл",
                "quantity": 1,
                "price": 250,
                "sum": 250,
                "vat_rate": "20",
            },
            {
                "position": 2,
                "name": "Зеленая греча",
                "quantity": 1,
                "price": 760,
                "sum": 760,
                "vat_rate": "10",
            },
        ],
        "fn": "OCR_HALLUCINATED_FN",
        "kkt_fn": "OCR_HALLUCINATED_FN",
    }
    resp = await client.post(
        "/api/receipts/",
        json={
            "date": "2026-05-26",
            "org": 'Ресторан "Мере"',
            "amount": 1010.0,
            "source": "photo_ocr",
            "raw_data": raw,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["org_inn"] == "7813679582"  # OCR INN now lands in the column
    assert body["org_legal"] == 'ООО "МЕРЕ"'
    assert body["org_brand"] == 'Ресторан "Мере"'
    assert body["operation_type"] == "purchase"
    assert body["payment_form"] == "card"
    assert body["tax_system"] == "osno"
    assert body["cashier"] == "Ботина Анастасия"
    # NDS-CLEANUP ②: у фото НДС одной суммой — ставку распознавание не даёт
    assert body["vat_total"] == 1110.00  # rubles — not /100
    assert body["vat_breakdown"] is None
    assert str(body["datetime"]).startswith("2026-05-26T12:41")
    assert body["kkt_fn"] is None  # Вариант A — OCR fn never stored

    items = [i for i in db.receipt_items if i["receipt_id"] == body["id"]]
    assert len(items) == 2
    assert items[0]["name"] == "Эспрессо 40мл"
    assert items[0]["sum"] == 250.0  # rubles
    assert items[0]["vat_rate"] == "20"  # string, not decoded


# ─── PATCH /api/receipts/{id} ─────────────────────────────────────────
async def test_patch_receipt_single_field(client, seeded):
    resp = await client.patch("/api/receipts/1", json={"payment": "Личная карта"})
    assert resp.status_code == 200
    assert resp.json()["payment"] == "Личная карта"
    assert resp.json()["org"] == "Лукойл"  # unchanged


async def test_patch_receipt_multiple_fields(client, seeded):
    resp = await client.patch(
        "/api/receipts/1", json={"category": "Прочее", "org": "Газпром"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["category_manual"] is True  # ручной выбор категории (вариант B)
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
    base = dict(
        id=1,
        date=date(2026, 5, 20),
        org="Some Org",
        category="Не указано",
        payment="Наличные",
        amount=500.0,
        employee=None,
        fn=None,
        kkt_fn=None,
        raw_data=None,
        source="manual",
        photo_url=None,
        org_id=1,
        category_id=None,
        category_manual=False,
        created_at=datetime.utcnow(),
    )
    base.update(over)
    db.receipts.append(base)
    db._rid = base["id"]


async def test_patch_category_resolves_id_and_sets_manual(client, db):
    await seed_default_categories(db, 1)
    _append_receipt(db)
    resp = await client.patch(
        "/api/receipts/1", json={"category": "Продукты для офиса"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["category_id"] == next(
        c["id"]
        for c in db.categories
        if c["org_id"] == 1 and c["name"] == "Продукты для офиса"
    )
    assert body["category_manual"] is True


async def test_patch_category_unknown_name_falls_back_id(client, db):
    await seed_default_categories(db, 1)
    _append_receipt(db)
    resp = await client.patch("/api/receipts/1", json={"category": "Несуществующая"})
    body = resp.json()
    # строки category в ответе нет (вариант B); неизвестное имя → category_id фолбэк
    # «Прочие хозрасходы» (per-org), флаг ручного выбора всё равно TRUE
    assert body["category_id"] == next(
        c["id"]
        for c in db.categories
        if c["org_id"] == 1 and c["name"] == "Прочие хозрасходы"
    )
    assert body["category_manual"] is True


async def test_patch_payment_keeps_category_manual_and_id(client, db):
    await seed_default_categories(db, 1)
    cid = next(
        c["id"] for c in db.categories if c["org_id"] == 1 and c["name"] == "Топливо"
    )
    _append_receipt(db, category="Топливо", category_id=cid, category_manual=False)
    resp = await client.patch("/api/receipts/1", json={"payment": "Личная карта"})
    body = resp.json()
    assert body["payment"] == "Личная карта"
    assert body["category_manual"] is False  # не трогаем при смене payment
    assert body["category_id"] == cid  # category_id не изменился


# ─── DELETE /api/receipts/{id} ────────────────────────────────────────
async def test_delete_receipt(client):
    created = await client.post(
        "/api/receipts/",
        json={"date": "2026-05-14", "org": "ВкусВилл", "amount": 800.0},
    )
    rid = created.json()["id"]

    resp = await client.delete(f"/api/receipts/{rid}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    remaining = (await client.get("/api/receipts/")).json()
    assert all(r["id"] != rid for r in remaining)


async def test_delete_receipt_cross_org_ignored(client, db):
    """Юзер org A (client=org_id=1) не может удалить чек org B: ответ 200 {"ok": True}
    (anti-enumeration), но чужой чек остаётся нетронутым (закрытие IDOR P1)."""
    _mk(db, 99, source="manual", org_id=2)  # чужая орг
    resp = await client.delete("/api/receipts/99")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert any(r["id"] == 99 for r in db.receipts)  # чужой чек жив


async def test_delete_receipt_org_safe_report_items(client, db):
    """При одиночном cross-org DELETE связь report_items чужой орг НЕ трогается
    (аналог test_bulk_delete_org_safe_report_items)."""
    _mk(db, 99, source="manual", org_id=2)
    db.report_items.append({"report_id": 5, "receipt_id": 99})  # связь чужого чека
    resp = await client.delete("/api/receipts/99")
    assert resp.status_code == 200
    assert any(ri["receipt_id"] == 99 for ri in db.report_items)  # уцелела


# ─── POST /api/receipts/bulk-delete (задача №9 фаза C) ────────────────
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
    _mk(db, 99, source="manual", org_id=2)  # чужая орг
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1, 99]})
    body = resp.json()
    assert body["deleted"] == [1]
    assert 99 not in body["deleted"] + body["blocked_fns"] + body["blocked_in_report"]
    assert any(r["id"] == 99 for r in db.receipts)  # чужой чек жив


async def test_bulk_delete_blocks_in_report(client, db):
    # Чек в отчёте блокируется ВСЕГДА, даже с force=true.
    _mk(db, 1, source="qr_scan", kkt_fn="FN-1")
    db.report_items.append({"report_id": 1, "receipt_id": 1})
    resp = await client.post(
        "/api/receipts/bulk-delete", json={"ids": [1], "force": True}
    )
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
    resp = await client.post(
        "/api/receipts/bulk-delete", json={"ids": [1], "force": True}
    )
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
    db.report_items.append({"report_id": 5, "receipt_id": 99})  # связь чужого чека
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1, 99]})
    assert resp.json()["deleted"] == [1]
    assert any(ri["receipt_id"] == 99 for ri in db.report_items)  # уцелела


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


# ─── GET /api/receipts/suggest-payment ────────────────────────────────
async def test_suggest_payment_returns_card(client, db, seeded):
    # T153 Ⓑ: подсказка ЛИЧНАЯ — засеянный чек приписывается спрашивающему,
    # иначе тест проверял бы старую, организационную политику.
    db.receipts[0]["user_id"] = 1
    resp = await client.get("/api/receipts/suggest-payment", params={"org": "Лукойл"})
    assert resp.status_code == 200
    assert resp.json()["payment"] == "Корп.карта"


async def test_suggest_payment_личная_а_не_чужая(client, db, seeded):
    """T153 Ⓑ: чужие привычки у продавца НЕ выбирают карту за меня.

    Владелец платил личной, форма тихо ставила корпоративную — потому что
    подсказка считалась по всей организации. Коллега с сотней чеков
    «Корп.карта» у того же продавца не должен перевешивать мою историю."""
    for i in range(3):
        db.receipts.append(
            dict(
                id=900 + i,
                org="Лукойл",
                payment="Личная 6645",
                date=__import__("datetime").date(2026, 8, 1 + i),
                amount=100.0,
                org_id=1,
                user_id=1,
                kkt_fn=None,
                raw_data=None,
                source="manual",
            )
        )
    for i in range(30):
        db.receipts.append(
            dict(
                id=950 + i,
                org="Лукойл",
                payment="Корп.карта 3950",
                date=__import__("datetime").date(2026, 7, 1 + i % 27),
                amount=100.0,
                org_id=1,
                user_id=777,
                kkt_fn=None,
                raw_data=None,
                source="manual",
            )
        )
    resp = await client.get("/api/receipts/suggest-payment", params={"org": "Лукойл"})
    assert resp.status_code == 200
    assert resp.json()["payment"] == "Личная 6645", (
        "тридцать чужих чеков перевесили три моих — подсказка не личная"
    )


async def test_suggest_payment_returns_null_when_no_history(client):
    resp = await client.get(
        "/api/receipts/suggest-payment", params={"org": "НеизвестнаяОрг"}
    )
    assert resp.status_code == 200
    assert resp.json()["payment"] is None


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


# ─── POST /api/receipts/  source + photo_url ──────────────────────────
async def test_create_receipt_defaults_source_to_manual(client):
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0}
    body = (await client.post("/api/receipts/", json=payload)).json()
    assert body["source"] == "manual"
    assert body["photo_url"] is None


async def test_create_receipt_honors_explicit_source(client):
    payload = {
        "date": "2026-05-17",
        "org": "Магнит",
        "amount": 100.0,
        "source": "qr_scan",
    }
    body = (await client.post("/api/receipts/", json=payload)).json()
    assert body["source"] == "qr_scan"


async def test_create_receipt_persists_photo_url(client):
    payload = {
        "date": "2026-05-17",
        "org": "Магнит",
        "amount": 100.0,
        "source": "photo_ocr",
        "photo_url": "https://r2.example/abc.jpg",
    }
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
    payload = {
        "date": "2026-05-17",
        "org": "Магнит",
        "amount": 100.0,
        "source": "photo_ocr",
        "raw_data": {"photo_base64": photo_b64, "items": []},
    }
    created = (await client.post("/api/receipts/", json=payload)).json()
    resp = await client.get(f"/api/receipts/{created['id']}/photo")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")
    assert resp.content == _PNG_1x1_BYTES


async def test_get_photo_redirects_when_photo_url_set(client):
    payload = {
        "date": "2026-05-17",
        "org": "Магнит",
        "amount": 100.0,
        "source": "photo_ocr",
        "photo_url": "https://r2.example/abc.jpg",
    }
    created = (await client.post("/api/receipts/", json=payload)).json()
    resp = await client.get(
        f"/api/receipts/{created['id']}/photo", follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://r2.example/abc.jpg"


async def test_get_photo_prefers_url_over_base64(client):
    """When both are present the external URL wins — R2 supersedes inline."""
    photo_b64 = _b64.b64encode(_PNG_1x1_BYTES).decode("ascii")
    payload = {
        "date": "2026-05-17",
        "org": "Магнит",
        "amount": 100.0,
        "source": "photo_ocr",
        "photo_url": "https://r2.example/abc.jpg",
        "raw_data": {"photo_base64": photo_b64},
    }
    created = (await client.post("/api/receipts/", json=payload)).json()
    resp = await client.get(
        f"/api/receipts/{created['id']}/photo", follow_redirects=False
    )
    assert resp.status_code == 302


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


# ═══════ МЕРА А (№25): КЛЮЧ ДЕДУПА ОТВЯЗАН ОТ ТОЧНОЙ ДАТЫ ═══════════
#
# ⚠️ ПОВОД — ЖИВОЙ СЛУЧАЙ 12.08.2026: ОДИН чек сохранён ТРИЖДЫ (id 71, 72,
# 73, все по 3500, все photo_ocr), приложение промолчало. Пары разошлись
# так: 71 и 72 — по ДАТЕ (владелец правил её руками), 72 и 73 — по
# ОРГАНИЗАЦИИ (ошибка распознавания). Точное равенство `date = $1`
# не совпало НИ РАЗУ, и мягкая ветка не сработала.
#
# ⚠️ ЗАМЕР ВЛАДЕЛЬЦА 28.08.2026 ПО КЛЮЧУ «ИНН + сумма + окно» ДАЛ НОЛЬ
# ЖИВЫХ ДУБЛЕЙ — И ЭТОТ НОЛЬ НИЧЕГО НЕ ДОКАЗЫВАЕТ: ключ замера тот же,
# что пропустил живой случай. Ноль означает «дублей, совпавших по трём
# полям, нет», а не «дублей нет».


async def test_мера_а_одна_сумма_разные_даты_ловится(client):
    """ПРИЁМОЧНЫЙ ЭТАЛОН: форма, на которой ключ молчал.

    Два чека одного поставщика на одну сумму с датами, разошедшимися
    на два дня, — ровно пара 71/72 с прода. При точном `date = $1`
    предупреждения не будет вовсе.
    """
    общее = {
        "org": 'ООО "МЕРКА"',
        "amount": 3500.0,
        "category": "Питание",
        "payment": "Наличные",
        "source": "photo_ocr",
        "org_inn": "7801696400",
    }
    первый = await client.post("/api/receipts/", json={**общее, "date": "2026-08-10"})
    assert первый.status_code == 200
    второй = await client.post("/api/receipts/", json={**общее, "date": "2026-08-12"})
    assert второй.status_code == 200, "это ПРЕДУПРЕЖДЕНИЕ, а не запрет"

    warning = второй.json().get("warning") or {}
    assert warning.get("duplicates"), (
        "ключ обязан поймать пару с РАЗНЫМИ датами: именно этой формой "
        "прошли id 71/72 на проде 12.08.2026"
    )


async def test_мера_а_дальше_окна_не_ловится(client):
    """⚠️ РАЗЛИЧАЮЩИЙ: ловит «сравнивать только сумму, дату выбросить».

    Без него тест выше прошёл бы и при полном отказе от даты, а это
    накрыло бы регулярные покупки за весь период. Десять дней — заведомо
    вне окна ±3.
    """
    общее = {
        "org": 'ООО "ГРИН КИНГ"',
        "amount": 490.0,
        "category": "Питание",
        "payment": "Карта",
        "source": "photo_ocr",
        "org_inn": "7801746812",
    }
    первый = await client.post("/api/receipts/", json={**общее, "date": "2026-08-02"})
    assert первый.status_code == 200
    второй = await client.post("/api/receipts/", json={**общее, "date": "2026-08-12"})
    assert второй.status_code == 200

    warning = второй.json().get("warning") or {}
    assert not warning.get("duplicates"), (
        "десять дней — вне окна ±3; иначе предупреждение накрыло бы "
        "регулярные покупки и научило бы жать «всё равно добавить» не глядя"
    )


async def test_мера_а_текст_называет_дату_чека(client):
    """ТРЕБОВАНИЕ ВЛАДЕЛЬЦА: не «такой чек уже есть», а с ДАТОЙ.

    Довод дословно: человек не помнит, сканировал он этот чек или нет,
    и предупреждение отвечает ровно на этот вопрос. С датой он РЕШАЕТ,
    без даты — гадает.

    ⚠️ Дата ЧЕКА, а не дата добавления: человек ищет в памяти покупку,
    а не своё действие в приложении.
    """
    общее = {
        "org": 'ООО "МЕРКА"',
        "amount": 1234.0,
        "category": "Питание",
        "payment": "Наличные",
        "source": "photo_ocr",
        "org_inn": "7801696400",
    }
    await client.post("/api/receipts/", json={**общее, "date": "2026-08-02"})
    второй = await client.post("/api/receipts/", json={**общее, "date": "2026-08-03"})

    сообщение = ((второй.json().get("warning") or {}).get("message")) or ""
    assert "2 августа" in сообщение, (
        f"сообщение обязано называть ДАТУ найденного чека, получено: {сообщение!r}"
    )
    assert "уже добавлен" in сообщение, (
        f"формулировка утвердительная, а не «возможно»: {сообщение!r}"
    )
