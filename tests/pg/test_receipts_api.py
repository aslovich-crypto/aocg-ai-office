# -*- coding: utf-8 -*-
"""Ручки чеков — НА ЖИВОЙ БАЗЕ (T36, перевод test_api.py, заход 1).

ПЕРЕВЕДЕНО С FakePool 06.09.2026. Тесты те же, прибор другой: список,
создание, все четыре ветки дедупа, предупреждения, разбор raw_data в колонки
и позиции, PATCH, DELETE, массовое удаление, подсказка оплаты, источник и
фото. Раньше это было 61 тестом внутри `tests/test_api.py` на двойнике.

ПОЧЕМУ ЧЕКИ ПЕРВЫМИ. Замер 06.09: из 125 веток FakePool 109 воспроизводят
отбор своими словами, и гуще всего — по чекам: 25 зеркал, из них 24 повторяют
условие доступа. Больше, чем у любой другой таблицы. Где гуще, там и начали.

⚠️ АВТОР ЧЕКА ОБЯЗАН СУЩЕСТВОВАТЬ. `receipts.user_id` — настоящий FK на
`users`, а создание чека пишет туда id смотрящего. У двойника чек с автором,
которого нет, ложился молча; здесь — нет. Отсюда автоиспользуемая фикстура
`смотрящий`: она заводит организацию 1 и админа id=1, того самого, кем
представляется фикстура `client`.
"""

from datetime import date

import pytest
import pytest_asyncio

from app.categories_seed import seed_default_categories

ADMIN_ID = 1  # client (admin) — user_id=1
ORG = 1


@pytest_asyncio.fixture(autouse=True)
async def смотрящий(db):
    """Тот, кем ходит `client`. Автоиспользуемая намеренно: почти каждый тест
    здесь создаёт чек ручкой, а ручка пишет автором id смотрящего — без строки
    в `users` живая база откажет по внешнему ключу. Пропускать её по одному
    тесту значило бы держать список исключений вместо правила."""
    await db.добавить_пользователя(id=ADMIN_ID, first_name="Админ", role="admin")
    return db


# ─── GET /api/receipts/ ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_receipts_returns_list(client):
    resp = await client.get("/api/receipts/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_get_receipts_with_data(client, seeded):
    resp = await client.get("/api/receipts/")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["org"] == "Лукойл"


# ─── POST /api/receipts/ ──────────────────────────────────────────────
@pytest.mark.asyncio
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
@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_dedup_branch_0_after_90s_allows(client, db):
    # Тот же чек, но первый создан > 90 сек назад → не двойной тап; в окне
    # 7 дней без ИНН → слабое предупреждение, чек создаётся.
    old = await db.момент(seconds=100)
    await db.добавить_чек(
        id=1,
        org="Кафе Уют",
        amount=6400.0,
        date=date(2026, 5, 21),
        payment="Наличные",
        kkt_fn=None,
        org_id=1,
        source="manual",
        org_inn=None,
        created_at=old,
    )
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
@pytest.mark.asyncio
async def test_dedup_strong_warning_photo_then_qr(client, db):
    # ГЛАВНЫЙ acceptance бага id3↔id4: photo_ocr создан первым (fn-less, ИНН в
    # колонке после Фикса №2), затем qr_scan того же чека → предупреждение, не
    # блок. Раньше qr_scan не видел photo_ocr-дубль (асимметрия C1).
    await db.добавить_чек(
        id=1,
        org='Ресторан "Мере"',
        amount=1010.0,
        date=date(2026, 5, 26),
        payment="Наличные",
        kkt_fn=None,
        org_id=1,
        source="photo_ocr",
        org_inn="7813679582",
        created_at=await db.момент(),
    )
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


@pytest.mark.asyncio
async def test_dedup_strong_warning_qr_then_photo(client, db):
    # Обратное направление: qr_scan (с fn) создан первым, затем photo_ocr (fn-less)
    # того же чека. Динамический fn-фильтр позволяет fn-less чеку найти fn-ный дубль.
    await db.добавить_чек(
        id=1,
        org='ООО "Мере"',
        amount=1010.0,
        date=date(2026, 5, 26),
        payment="Наличные",
        kkt_fn="7380440902249741",
        org_id=1,
        source="qr_scan",
        org_inn="7813679582",
        created_at=await db.момент(),
    )
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


@pytest.mark.asyncio
async def test_dedup_window_7_days_strong_warning(client, db):
    # Сильный ключ ловит дубль в окне 7 дней (создан 6 дней назад).
    await db.добавить_чек(
        id=1,
        org='ООО "Мере"',
        amount=1010.0,
        date=date(2026, 5, 26),
        payment="Наличные",
        kkt_fn=None,
        org_id=1,
        source="photo_ocr",
        org_inn="7813679582",
        created_at=await db.момент(days=6),
    )
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


@pytest.mark.asyncio
async def test_dedup_outside_7_days_no_warning(client, db):
    # Старше 7 дней → вне окна, предупреждения нет.
    await db.добавить_чек(
        id=1,
        org='ООО "Мере"',
        amount=1010.0,
        date=date(2026, 5, 26),
        payment="Наличные",
        kkt_fn=None,
        org_id=1,
        source="photo_ocr",
        org_inn="7813679582",
        created_at=await db.момент(days=8),
    )
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
@pytest.mark.asyncio
async def test_dedup_weak_warning_no_inn(client, db):
    old = await db.момент(hours=2)  # вне 90 сек, в окне 7 дней
    await db.добавить_чек(
        id=1,
        org="Ларёк",
        amount=500.0,
        date=date(2026, 5, 21),
        payment="Наличные",
        kkt_fn=None,
        org_id=1,
        source="manual",
        org_inn=None,
        created_at=old,
    )
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


@pytest.mark.asyncio
async def test_dedup_invalid_inn_falls_to_weak(client, db):
    # Невалидный ИНН отфильтрован парсером ФНС (org_inn=None) → слабая ветка.
    await db.добавить_чек(
        id=1,
        org="Кафе",
        amount=700.0,
        date=date(2026, 5, 21),
        payment="Наличные",
        kkt_fn=None,
        org_id=1,
        source="photo_ocr",
        org_inn=None,
        created_at=await db.момент(hours=1),
    )
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
@pytest.mark.asyncio
async def test_dedup_category_and_payment_not_in_key(client, db):
    # У сохранённого чека category/payment отличаются от нового — предупреждение
    # всё равно срабатывает (в ключ входят только date+amount+ИНН).
    await db.добавить_чек(
        id=1,
        org='ООО "Мере"',
        amount=1010.0,
        date=date(2026, 5, 26),
        payment="Корп.карта",
        kkt_fn="FN-1",
        org_id=1,
        source="qr_scan",
        org_inn="7813679582",
        created_at=await db.момент(),
    )
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


@pytest.mark.asyncio
async def test_dedup_patch_change_doesnt_break_dedup(client, db):
    # Вариант 3 из диагностики: пользователь меняет category через PATCH ПОСЛЕ
    # создания. Раньше это рассинхронизировало composite-ключ; теперь category
    # не в ключе, поэтому последующий дубль по date+amount+ИНН ловится.
    await db.добавить_чек(
        id=1,
        org='ООО "Мере"',
        amount=1010.0,
        date=date(2026, 5, 26),
        payment="Наличные",
        kkt_fn=None,
        org_id=1,
        source="photo_ocr",
        org_inn="7813679582",
        created_at=await db.момент(),
    )
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
@pytest.mark.asyncio
async def test_warning_similar_receipt_includes_all_fields(client, db):
    # similar_receipt должен содержать {id, amount, org, date} в правильных
    # JSON-типах: id=int, org=str, amount=float, date=str ISO ("YYYY-MM-DD").
    await db.добавить_чек(
        id=1,
        org='ООО "Мере"',
        amount=1010.0,
        date=date(2026, 5, 26),
        payment="Наличные",
        kkt_fn=None,
        org_id=1,
        source="photo_ocr",
        org_inn="7813679582",
        created_at=await db.момент(),
    )
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


@pytest.mark.asyncio
async def test_warning_backward_compat_id_field(client, db):
    # similar_receipt_id (deprecated) сохраняется параллельно similar_receipt —
    # старый фронт, читающий только id, не ломается.
    await db.добавить_чек(
        id=1,
        org="Ларёк",
        amount=500.0,
        date=date(2026, 5, 21),
        payment="Наличные",
        kkt_fn=None,
        org_id=1,
        source="manual",
        org_inn=None,
        created_at=await db.момент(hours=2),
    )
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
async def _seed_photo_dup(db, *, in_report=False):
    """Существующий photo_ocr-чек (fn-less, ИНН в колонке) за 5 мин до нового."""
    await db.добавить_чек(
        id=1,
        org='ООО "Мере"',
        amount=1010.0,
        date=date(2026, 5, 26),
        payment="Наличные",
        kkt_fn=None,
        org_id=1,
        source="photo_ocr",
        org_inn="7813679582",
        created_at=await db.момент(minutes=5),
    )
    if in_report:
        # ⚠️ Оба конца связи — настоящие FK. У двойника хватало словаря
        # {"report_id": 1, "receipt_id": 1}, и отчёта №1 могло не быть вовсе.
        await db.добавить_отчёт(id=1, title="Отчёт", user_id=ADMIN_ID)
        await db.положить_в_отчёт(1, 1)


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


@pytest.mark.asyncio
async def test_warning_duplicates_includes_array(client, db):
    await _seed_photo_dup(db)
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


@pytest.mark.asyncio
async def test_warning_duplicates_includes_new_receipt(client, db):
    await _seed_photo_dup(db)
    resp = await _post_qr_dup(client)
    dups = resp.json()["warning"]["duplicates"]
    new = [d for d in dups if d["is_new"]]
    assert len(new) == 1 and new[0]["id"] == resp.json()["id"]
    assert new[0]["source"] == "qr_scan"
    assert sum(1 for d in dups if not d["is_new"]) == 1


@pytest.mark.asyncio
async def test_warning_duplicates_marks_deletable(client, db):
    # photo_ocr (kkt_fn NULL) → deletable True; qr_scan (kkt_fn) → deletable False.
    await _seed_photo_dup(db)
    resp = await _post_qr_dup(client)
    dups = {d["id"]: d for d in resp.json()["warning"]["duplicates"]}
    assert dups[1]["deletable"] is True
    assert dups[resp.json()["id"]]["deletable"] is False


@pytest.mark.asyncio
async def test_warning_duplicates_marks_in_report(client, db):
    await _seed_photo_dup(db, in_report=True)  # id=1 уже в отчёте
    resp = await _post_qr_dup(client)
    dups = {d["id"]: d for d in resp.json()["warning"]["duplicates"]}
    assert dups[1]["in_report"] is True
    assert dups[resp.json()["id"]]["in_report"] is False  # только что создан


# ─── (ФН, ФД) UniqueViolation guard: cross-org collision -> 409 ──────
@pytest.mark.asyncio
async def test_unique_violation_kkt_fn_cross_org_returns_409(client, db):
    # SELECT-дедуп per-org (WHERE kkt_fn=$1 AND fd_num=$2 AND org_id=$3), а индекс
    # receipts_kkt_fn_fd_unique — ГЛОБАЛЬНЫЙ по паре (ФН, ФД). Тот же документ
    # (ФН+ФД) уже есть в другой org (org_id=2). Пост в org 1 промахивается мимо
    # per-org дедупа, доходит до INSERT, ловится глобальным индексом → 409.
    await db.добавить_чек(
        id=99,
        org="Чужая Орг",
        amount=10.0,
        date=date(2026, 5, 1),
        payment=None,
        kkt_fn="GLOBAL-X",
        fd_num="555",
        org_id=2,
        source="qr_scan",
        created_at=await db.момент(),
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


@pytest.mark.asyncio
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
@pytest.mark.asyncio
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

    items = await db.позиции_чека(body["id"])
    assert len(items) == 2
    assert items[0]["name"] == "Аспирин"
    assert items[0]["sum"] == 2000.0
    assert items[0]["vat_rate"] == "20"


# ─── photo_ocr: OCR raw_data parsed into typed columns + receipt_items ─
@pytest.mark.asyncio
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
    # ⚠️ РАЗНОГЛАСИЕ ЗЕРКАЛА И БАЗЫ, найдено переводом 06.09.2026.
    # Двойник возвращал ровно ту строку, что положили, и проверка
    # `startswith("2026-05-26T12:41")` была зелёной по построению. Колонка
    # `receipts.datetime` — TIMESTAMPTZ, распознавание отдаёт время БЕЗ ПОЯСА
    # («12:41» на бумажном чеке), и PostgreSQL толкует его в поясе СВОЕЙ сессии.
    #
    # ⚠️ НА ПРОДЕ ЭТО НЕ ДЕФЕКТ: кластер там в UTC (замер 06.09 через бастион),
    # и 12:41 читается как 12:41. Расходится ЛОКАЛЬНЫЙ кластер, поднятый в поясе
    # системы: в Europe/Moscow тот же чек читается как 09:41+00:00. Проверка
    # обязана быть верна в ОБЕИХ средах, поэтому проверяем не текст, а суть:
    # ответ ручки совпадает с тем, что лежит в колонке, а по часам сервера на
    # чеке действительно 12:41.
    из_базы = (await db.чек(body["id"]))["datetime"]
    assert body["datetime"] == из_базы.isoformat()
    по_часам_сервера = await db.pool.fetchval(
        "SELECT $1::timestamptz AT TIME ZONE current_setting('TimeZone')", из_базы
    )
    assert по_часам_сервера.isoformat().startswith("2026-05-26T12:41")
    assert body["kkt_fn"] is None  # Вариант A — OCR fn never stored

    items = await db.позиции_чека(body["id"])
    assert len(items) == 2
    assert items[0]["name"] == "Эспрессо 40мл"
    assert items[0]["sum"] == 250.0  # rubles
    assert items[0]["vat_rate"] == "20"  # string, not decoded


# ─── PATCH /api/receipts/{id} ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_patch_receipt_single_field(client, seeded):
    resp = await client.patch("/api/receipts/1", json={"payment": "Личная карта"})
    assert resp.status_code == 200
    assert resp.json()["payment"] == "Личная карта"
    assert resp.json()["org"] == "Лукойл"  # unchanged


@pytest.mark.asyncio
async def test_patch_receipt_multiple_fields(client, seeded):
    resp = await client.patch(
        "/api/receipts/1", json={"category": "Прочее", "org": "Газпром"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["category_manual"] is True  # ручной выбор категории (вариант B)
    assert body["org"] == "Газпром"


@pytest.mark.asyncio
async def test_patch_receipt_no_fields_returns_existing(client, seeded):
    resp = await client.patch("/api/receipts/1", json={})
    assert resp.status_code == 200
    assert resp.json()["org"] == "Лукойл"


@pytest.mark.asyncio
async def test_patch_receipt_not_found(client):
    resp = await client.patch("/api/receipts/999", json={"category": "X"})
    assert resp.status_code == 404


# ─── Смена категории чека: category_id резолвится + category_manual=TRUE ───
async def _append_receipt(db, **over):
    поля = dict(
        id=1,
        date=date(2026, 5, 20),
        org="Some Org",
        payment="Наличные",
        amount=500.0,
        org_id=1,
        source="manual",
        created_at=await db.момент(),
    )
    поля.update(over)
    await db.добавить_чек(**поля)


@pytest.mark.asyncio
async def test_patch_category_resolves_id_and_sets_manual(client, db):
    await seed_default_categories(db.pool, ORG)
    await _append_receipt(db)
    resp = await client.patch(
        "/api/receipts/1", json={"category": "Продукты для офиса"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["category_id"] == await db.id_категории("Продукты для офиса")
    assert body["category_manual"] is True


@pytest.mark.asyncio
async def test_patch_category_unknown_name_falls_back_id(client, db):
    await seed_default_categories(db.pool, ORG)
    await _append_receipt(db)
    resp = await client.patch("/api/receipts/1", json={"category": "Несуществующая"})
    body = resp.json()
    # строки category в ответе нет (вариант B); неизвестное имя → category_id фолбэк
    # «Прочие хозрасходы» (per-org), флаг ручного выбора всё равно TRUE
    assert body["category_id"] == await db.id_категории("Прочие хозрасходы")
    assert body["category_manual"] is True


@pytest.mark.asyncio
async def test_patch_payment_keeps_category_manual_and_id(client, db):
    await seed_default_categories(db.pool, ORG)
    cid = await db.id_категории("Топливо")
    await _append_receipt(db, category_id=cid)
    resp = await client.patch("/api/receipts/1", json={"payment": "Личная карта"})
    body = resp.json()
    assert body["payment"] == "Личная карта"
    assert body["category_manual"] is False  # не трогаем при смене payment
    assert body["category_id"] == cid  # category_id не изменился


# ─── DELETE /api/receipts/{id} ────────────────────────────────────────
@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_delete_receipt_cross_org_ignored(client, db):
    """Юзер org A (client=org_id=1) не может удалить чек org B: ответ 200 {"ok": True}
    (anti-enumeration), но чужой чек остаётся нетронутым (закрытие IDOR P1)."""
    await _mk(db, 99, source="manual", org_id=2)  # чужая орг
    resp = await client.delete("/api/receipts/99")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert await db.чек(99) is not None  # чужой чек жив


@pytest.mark.asyncio
async def test_delete_receipt_org_safe_report_items(client, db):
    """При одиночном cross-org DELETE связь report_items чужой орг НЕ трогается
    (аналог test_bulk_delete_org_safe_report_items)."""
    await _mk(db, 99, source="manual", org_id=2)
    # ⚠️ Оба конца связи — FK: отчёт №5 обязан существовать. У двойника
    # связь висела в пустоте, и тест описывал состояние, которого не бывает.
    await db.добавить_отчёт(id=5, title="Чужой отчёт", user_id=ADMIN_ID, org_id=2)
    await db.положить_в_отчёт(5, 99)  # связь чужого чека
    resp = await client.delete("/api/receipts/99")
    assert resp.status_code == 200
    assert {"report_id": 5, "receipt_id": 99} in await db.связи_отчётов()


# ─── POST /api/receipts/bulk-delete (задача №9 фаза C) ────────────────
async def _mk(
    db, rid, *, source="manual", kkt_fn=None, org_id=1, amount=100.0, user_id=1
):
    await db.добавить_чек(
        id=rid,
        org=f"Org{rid}",
        amount=amount,
        date=date(2026, 5, 20),
        payment=None,
        kkt_fn=kkt_fn,
        org_id=org_id,
        user_id=user_id,
        source=source,
        created_at=await db.момент(),
    )


@pytest.mark.asyncio
async def test_bulk_delete_basic(client, db):
    await _mk(db, 1, source="manual")
    await _mk(db, 2, source="photo_ocr")
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1, 2]})
    assert resp.status_code == 200
    body = resp.json()
    assert sorted(body["deleted"]) == [1, 2]
    assert body["blocked_fns"] == [] and body["blocked_in_report"] == []
    assert await db.чеки() == []


@pytest.mark.asyncio
async def test_bulk_delete_cross_org_ignored(client, db):
    await _mk(db, 1, source="manual", org_id=1)
    await _mk(db, 99, source="manual", org_id=2)  # чужая орг
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1, 99]})
    body = resp.json()
    assert body["deleted"] == [1]
    assert 99 not in body["deleted"] + body["blocked_fns"] + body["blocked_in_report"]
    assert await db.чек(99) is not None  # чужой чек жив


@pytest.mark.asyncio
async def test_bulk_delete_blocks_in_report(client, db):
    # Чек в отчёте блокируется ВСЕГДА, даже с force=true.
    await _mk(db, 1, source="qr_scan", kkt_fn="FN-1")
    # ⚠️ ОТЧЁТ ЗАВОДИМ НАСТОЯЩИЙ, А НЕ ОДНУ СВЯЗЬ. На живой базе report_items
    # ссылается на reports внешним ключом, и связь без отчёта не существует;
    # двойник её допускал, и тест описывал состояние, которого не бывает.
    await db.добавить_отчёт(
        id=1,
        title="Отчёт с чеком",
        user_id=ADMIN_ID,
        total=0,
        status="Черновик",
        org_id=ORG,
        created=date(2026, 7, 1),
    )
    await db.положить_в_отчёт(1, 1)
    resp = await client.post(
        "/api/receipts/bulk-delete", json={"ids": [1], "force": True}
    )
    body = resp.json()
    assert body["blocked_in_report"] == [1]
    assert body["deleted"] == [] and body["blocked_fns"] == []
    assert await db.чек(1) is not None


@pytest.mark.asyncio
async def test_bulk_delete_blocks_fns_without_force(client, db):
    await _mk(db, 1, source="qr_scan", kkt_fn="FN-1")
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1]})
    body = resp.json()
    assert body["blocked_fns"] == [1]
    assert body["deleted"] == []
    assert await db.чек(1) is not None


@pytest.mark.asyncio
async def test_bulk_delete_force_fns_succeeds(client, db):
    await _mk(db, 1, source="qr_scan", kkt_fn="FN-1")
    resp = await client.post(
        "/api/receipts/bulk-delete", json={"ids": [1], "force": True}
    )
    body = resp.json()
    assert body["deleted"] == [1] and body["blocked_fns"] == []
    assert await db.чеки() == []


@pytest.mark.asyncio
async def test_bulk_delete_mixed_response(client, db):
    # 1 manual → удалить; 2 qr_scan без force → blocked_fns; 3 в отчёте → blocked_in_report.
    await _mk(db, 1, source="manual")
    await _mk(db, 2, source="qr_scan", kkt_fn="F2")
    await _mk(db, 3, source="photo_ocr")
    # Тот же довод: связь живёт только при существующем отчёте (FK на живой базе).
    await db.добавить_отчёт(
        id=1,
        title="Отчёт с чеком",
        user_id=ADMIN_ID,
        total=0,
        status="Черновик",
        org_id=ORG,
        created=date(2026, 7, 1),
    )
    await db.положить_в_отчёт(1, 3)
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1, 2, 3]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == [1]
    assert body["blocked_fns"] == [2]
    assert body["blocked_in_report"] == [3]
    assert {r["id"] for r in await db.чеки()} == {2, 3}


@pytest.mark.asyncio
async def test_bulk_delete_org_safe_report_items(client, db):
    # Связь report_items чужого чека (org_id=2) НЕ трогается, даже если id передан.
    await _mk(db, 1, source="manual", org_id=1)
    await _mk(db, 99, source="manual", org_id=2)
    # ⚠️ Оба конца связи — FK: отчёт №5 обязан существовать. У двойника
    # связь висела в пустоте, и тест описывал состояние, которого не бывает.
    await db.добавить_отчёт(id=5, title="Чужой отчёт", user_id=ADMIN_ID, org_id=2)
    await db.положить_в_отчёт(5, 99)  # связь чужого чека
    resp = await client.post("/api/receipts/bulk-delete", json={"ids": [1, 99]})
    assert resp.json()["deleted"] == [1]
    assert {"report_id": 5, "receipt_id": 99} in await db.связи_отчётов()


# ─── GET /api/receipts/suggest-payment ────────────────────────────────
@pytest.mark.asyncio
async def test_suggest_payment_returns_card(client, db, seeded):
    # T153 Ⓑ: подсказка ЛИЧНАЯ — засеянный чек приписывается спрашивающему,
    # иначе тест проверял бы старую, организационную политику.
    await db.назначить_автора(1, ADMIN_ID)
    resp = await client.get("/api/receipts/suggest-payment", params={"org": "Лукойл"})
    assert resp.status_code == 200
    assert resp.json()["payment"] == "Корп.карта"


@pytest.mark.asyncio
async def test_suggest_payment_личная_а_не_чужая(client, db, seeded):
    """T153 Ⓑ: чужие привычки у продавца НЕ выбирают карту за меня.

    Владелец платил личной, форма тихо ставила корпоративную — потому что
    подсказка считалась по всей организации. Коллега с сотней чеков
    «Корп.карта» у того же продавца не должен перевешивать мою историю."""
    for i in range(3):
        await db.добавить_чек(
            id=900 + i,
            org="Лукойл",
            amount=100.0,
            date=__import__("datetime").date(2026, 8, 1 + i),
            payment="Личная 6645",
            kkt_fn=None,
            org_id=1,
            user_id=1,
            source="manual",
        )
    # ⚠️ КОЛЛЕГА ОБЯЗАН СУЩЕСТВОВАТЬ. `receipts.user_id` — настоящий FK;
    # у двойника тридцать чеков висели на сотруднике №777, которого в
    # системе нет, и тест описывал состояние, которого не бывает.
    await db.добавить_пользователя(id=777, first_name="Коллега", role="employee")
    for i in range(30):
        await db.добавить_чек(
            id=950 + i,
            org="Лукойл",
            amount=100.0,
            date=__import__("datetime").date(2026, 7, 1 + i % 27),
            payment="Корп.карта 3950",
            kkt_fn=None,
            org_id=1,
            user_id=777,
            source="manual",
        )
    resp = await client.get("/api/receipts/suggest-payment", params={"org": "Лукойл"})
    assert resp.status_code == 200
    assert resp.json()["payment"] == "Личная 6645", (
        "тридцать чужих чеков перевесили три моих — подсказка не личная"
    )


@pytest.mark.asyncio
async def test_suggest_payment_returns_null_when_no_history(client):
    resp = await client.get(
        "/api/receipts/suggest-payment", params={"org": "НеизвестнаяОрг"}
    )
    assert resp.status_code == 200
    assert resp.json()["payment"] is None


# ─── POST /api/receipts/  source + photo_url ──────────────────────────
@pytest.mark.asyncio
async def test_create_receipt_defaults_source_to_manual(client):
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0}
    body = (await client.post("/api/receipts/", json=payload)).json()
    assert body["source"] == "manual"
    assert body["photo_url"] is None


@pytest.mark.asyncio
async def test_create_receipt_honors_explicit_source(client):
    payload = {
        "date": "2026-05-17",
        "org": "Магнит",
        "amount": 100.0,
        "source": "qr_scan",
    }
    body = (await client.post("/api/receipts/", json=payload)).json()
    assert body["source"] == "qr_scan"


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_get_photo_404_when_receipt_missing(client):
    resp = await client.get("/api/receipts/9999/photo")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_photo_404_when_no_photo(client):
    payload = {"date": "2026-05-17", "org": "Магнит", "amount": 100.0}
    created = (await client.post("/api/receipts/", json=payload)).json()
    resp = await client.get(f"/api/receipts/{created['id']}/photo")
    assert resp.status_code == 404


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
