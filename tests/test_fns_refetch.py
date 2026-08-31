# -*- coding: utf-8 -*-
"""Дозапрос в ФНС по сохранённому чеку (T132).

⚠️ ЗАЧЕМ ЭТИ ТЕСТЫ ВООБЩЕ. 31.08.2026 налоговая перестала отдавать данные:
код 5 «нет информации» приходил на ЗАРЕГИСТРИРОВАННЫЕ чеки. Чек в такой день
сохраняется по фото, с пустыми позициями и НДС, и **до T132 оставался пустым
навсегда**: перезапроса не было ни одного, а строки QR, которой он делается,
нигде не хранилось.

⚠️ ГЛАВНОЕ, ЧТО ЗДЕСЬ СТЕРЕЖЁТСЯ, — НЕ «РАБОТАЕТ ЛИ ЗАПРОС», А **ЧТО ОН НЕ
ЗАТИРАЕТ ВВЕДЁННОЕ РУКАМИ**. Человек, пока ждал налоговую, мог вписать
название и сумму сам. Ответ пришёл позже — но это не делает его главнее.
"""

import pytest

from app.routers import fns


@pytest.mark.asyncio
async def test_дозапрос_заполняет_ТОЛЬКО_пустые_поля(client, db, monkeypatch):
    """Введённое человеком остаётся, пустое дозаполняется."""
    db.receipts.append(
        dict(
            id=90,
            date=None,
            org="Вписал руками",
            org_legal="ООО Вписал Руками",  # ← это трогать нельзя
            amount=1440.0,
            org_id=1,
            user_id=1,
            source="qr_scan",
            kkt_fn="7380440902762965",
            fd_num="19368",
            fpd="1560710082",
            raw_data=None,
            datetime=__import__("datetime").datetime(2026, 8, 31, 17, 28),
            operation_type="purchase",
            address=None,
        )
    )

    async def подстава(_qr):
        return 200, {
            "status": "ok",
            "raw": {
                "user": "ООО Ромашка",
                "userInn": "7701234567",
                "retailPlaceAddress": "Москва, Тверская 1",
                "totalSum": 144000,
                "items": [],
            },
        }

    monkeypatch.setattr(fns, "спросить_фнс", подстава)
    import app.routers.receipts as receipts_module

    monkeypatch.setattr(receipts_module, "спросить_фнс", подстава)

    r = await client.post("/api/receipts/90/refetch-fns")
    assert r.status_code == 200, r.text
    строка = [c for c in db.receipts if c["id"] == 90][0]
    assert строка["org_legal"] == "ООО Вписал Руками", (
        "ответ ФНС затёр введённое руками"
    )
    assert строка["address"] == "Москва, Тверская 1", "пустое поле не заполнилось"
    assert строка["raw_data"], "raw_data обязан появиться — по нему снимается признак"


@pytest.mark.asyncio
async def test_старый_чек_дозапросить_нельзя_и_сказано_почему(client, db):
    """⚠️ Старые чеки не трогаем (решение владельца), но отказ обязан назвать
    причину и следующий шаг, а не сказать «нельзя»."""
    db.receipts.append(
        dict(
            id=91,
            date=None,
            org="Старый",
            amount=100.0,
            org_id=1,
            user_id=1,
            source="photo_ocr",
            kkt_fn=None,
            fd_num=None,
            fpd=None,
            raw_data=None,
            datetime=None,
            operation_type="purchase",
        )
    )
    r = await client.post("/api/receipts/91/refetch-fns")
    assert r.status_code == 409, r.text
    assert "ФД и ФПД" in r.json()["detail"]


@pytest.mark.asyncio
async def test_чужой_чек_дозапросить_нельзя(client, db):
    """org-scope: дозапрос не должен быть дырой в чужую организацию."""
    db.receipts.append(
        dict(
            id=92,
            date=None,
            org="Чужой",
            amount=1.0,
            org_id=777,
            user_id=1,
            source="qr_scan",
            kkt_fn="1",
            fd_num="1",
            fpd="1",
            raw_data=None,
            datetime=__import__("datetime").datetime(2026, 8, 31, 12, 0),
            operation_type="purchase",
        )
    )
    r = await client.post("/api/receipts/92/refetch-fns")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_признак_можно_дозапросить_считает_ТОЛЬКО_подходящие(client, db):
    """⚠️ БЕЗ ЭТОГО ТЕСТА КНОПКА ПОЯВЛЯЛАСЬ БЫ ГДЕ ПОПАЛО. Признак выводится,
    а не хранится, и правило у него одно: три поля есть, raw_data пусто.
    Старый чек (без ФД и ФПД) под условие не подпадает САМ — так владелец
    и решил: старые не помечаем и не трогаем."""
    from datetime import datetime as дт

    db.receipts.append(
        dict(id=93, date=None, org="Ждёт ФНС", amount=10.0, org_id=1, user_id=1,
             source="qr_scan", kkt_fn="1", fd_num="2", fpd="3", raw_data=None,
             datetime=дт(2026, 8, 31, 10, 0), operation_type="purchase")
    )
    db.receipts.append(
        dict(id=94, date=None, org="Старый", amount=10.0, org_id=1, user_id=1,
             source="photo_ocr", kkt_fn=None, fd_num=None, fpd=None, raw_data=None,
             datetime=дт(2026, 8, 31, 10, 0), operation_type="purchase")
    )
    db.receipts.append(
        dict(id=95, date=None, org="Полный", amount=10.0, org_id=1, user_id=1,
             source="qr_scan", kkt_fn="1", fd_num="2", fpd="3",
             raw_data={"user": "ООО"}, datetime=дт(2026, 8, 31, 10, 0),
             operation_type="purchase")
    )
    r = await client.get("/api/receipts/")
    assert r.status_code == 200, r.text
    по_id = {c["id"]: c for c in r.json()}
    assert по_id[93]["можно_дозапросить"] is True, "ждёт ФНС — кнопка нужна"
    assert по_id[94]["можно_дозапросить"] is False, "старый чек не помечаем"
    assert по_id[95]["можно_дозапросить"] is False, "данные уже получены"
