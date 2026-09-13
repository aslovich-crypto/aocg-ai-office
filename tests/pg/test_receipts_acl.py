# -*- coding: utf-8 -*-
"""A-ACL: разграничение доступа к чекам по автору и роли — НА ЖИВОЙ БАЗЕ.

ПЕРЕВЕДЕНО С FakePool 03.09.2026 (T36, сессия 2). Тесты те же, прибор другой.

Модель:
- VIEW + PATCH: employee — только свои; accountant/admin — все в орг.
- DELETE/bulk: employee/accountant — только свои; admin — любые в орг.
- dedupe-cleanup — только admin.
Фикстуры (tests/pg/conftest): client=admin(id=1), client_accountant(id=1),
client_employee(id=2).
"""

from datetime import date

import pytest
import pytest_asyncio

ADMIN_ID = 1  # client (admin) и client_accountant используют user_id=1
EMP_ID = 2  # client_employee — user_id=2
ORG = 1


@pytest_asyncio.fixture
async def люди(db):
    """Авторы чеков. У двойника чек с любым user_id жил сам по себе —
    на живой базе receipts.user_id это настоящий FK, автор обязан
    существовать, как и в проде."""
    await db.добавить_пользователя(id=ADMIN_ID, first_name="Админ", role="admin")
    await db.добавить_пользователя(id=EMP_ID, first_name="Иван", role="employee")
    return db


async def _чек(db, rid, user_id, org_id=ORG):
    """Чек с заданным автором (минимум полей для эндпоинтов)."""
    await db.добавить_чек(
        id=rid,
        org="ООО Тест",
        amount=100,
        date=date(2026, 6, 1),
        payment="Карта",
        org_id=org_id,
        user_id=user_id,
    )


# ── VIEW ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_employee_listing_sees_only_own(db, люди, client_employee):
    await _чек(db, 101, ADMIN_ID)  # чужой
    await _чек(db, 102, EMP_ID)  # свой
    r = await client_employee.get("/api/receipts/")
    assert r.status_code == 200
    assert {x["id"] for x in r.json()} == {102}


@pytest.mark.asyncio
async def test_accountant_listing_sees_all(db, люди, client_accountant):
    await _чек(db, 101, ADMIN_ID)
    await _чек(db, 102, EMP_ID)
    r = await client_accountant.get("/api/receipts/")
    assert {x["id"] for x in r.json()} == {101, 102}


@pytest.mark.asyncio
async def test_admin_listing_sees_all(db, люди, client):
    await _чек(db, 101, ADMIN_ID)
    await _чек(db, 102, EMP_ID)
    r = await client.get("/api/receipts/")
    assert {x["id"] for x in r.json()} == {101, 102}


@pytest.mark.asyncio
async def test_employee_get_foreign_404_own_200(db, люди, client_employee):
    await _чек(db, 101, ADMIN_ID)  # чужой
    await _чек(db, 102, EMP_ID)  # свой
    assert (await client_employee.get("/api/receipts/101")).status_code == 404
    assert (await client_employee.get("/api/receipts/102")).status_code == 200


# ── PATCH ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_employee_patch_foreign_404(db, люди, client_employee):
    await _чек(db, 101, ADMIN_ID)  # чужой
    r = await client_employee.patch("/api/receipts/101", json={"payment": "Нал"})
    assert r.status_code == 404
    assert (await db.чек(101))["payment"] == "Карта"  # не изменён


@pytest.mark.asyncio
async def test_accountant_patch_foreign_ok(db, люди, client_accountant):
    await _чек(db, 102, EMP_ID)  # автор employee — правит accountant
    r = await client_accountant.patch("/api/receipts/102", json={"payment": "Нал"})
    assert r.status_code == 200
    assert (await db.чек(102))["payment"] == "Нал"


# ── DELETE ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_employee_delete_foreign_noop(db, люди, client_employee):
    await _чек(db, 101, ADMIN_ID)  # чужой
    r = await client_employee.delete("/api/receipts/101")
    assert r.status_code == 200  # anti-enum: всегда 200
    assert await db.чек(101) is not None  # но не удалён


@pytest.mark.asyncio
async def test_accountant_delete_foreign_noop(db, люди, client_accountant):
    await _чек(db, 102, EMP_ID)  # автор employee
    r = await client_accountant.delete("/api/receipts/102")
    assert r.status_code == 200
    assert await db.чек(102) is not None  # accountant НЕ удаляет чужой


@pytest.mark.asyncio
async def test_admin_delete_foreign_ok(db, люди, client):
    await _чек(db, 102, EMP_ID)  # автор employee — удаляет admin
    r = await client.delete("/api/receipts/102")
    assert r.status_code == 200
    assert await db.чек(102) is None  # удалён


# ── bulk-delete + dedupe-cleanup ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_bulk_delete_employee_ignores_foreign(db, люди, client_employee):
    await _чек(db, 101, ADMIN_ID)  # чужой
    await _чек(db, 102, EMP_ID)  # свой
    r = await client_employee.post(
        "/api/receipts/bulk-delete", json={"ids": [101, 102]}
    )
    assert r.status_code == 200
    assert r.json()["deleted"] == [102]
    assert await db.чек(101) is not None
    assert await db.чек(102) is None


@pytest.mark.asyncio
async def test_dedupe_cleanup_403_for_non_admin(db, люди, client_employee):
    r = await client_employee.post("/api/receipts/dedupe-cleanup/")
    assert r.status_code == 403


# ─────────────────────────── СНИМОК ЧЕКА ───────────────────────────
# ⚠️ ЭТУ ПАРУ НЕ СТЕРЁГ НИКТО, И НАШЁЛ ЭТО АУДИТ ЗАХОДА ⑥ (13.09.2026).
# Живые проверки ручки снимка были, но все ходили под админом: ветка
# «сотрудник видит только свой» не исполнялась ни разу — ни здесь, ни
# на двойнике. Условие отбора исполняет SQL, значит и проверять его надо
# на живой базе, а не на зеркале.


async def _чек_со_снимком(db, rid, user_id, org_id=ORG):
    await _чек(db, rid, user_id, org_id)
    await db.pool.execute(
        "UPDATE receipts SET raw_data=$2 WHERE id=$1",
        rid,
        # Однопиксельный PNG: важен не рисунок, а то, что байты пришли.
        {
            "photo_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lE"
            "QVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        },
    )


@pytest.mark.asyncio
async def test_employee_photo_own_ok(db, люди, client_employee):
    await _чек_со_снимком(db, 1, EMP_ID)
    resp = await client_employee.get("/api/receipts/1/photo")
    assert resp.status_code == 200, resp.text
    assert resp.content, "снимок пришёл пустым"


@pytest.mark.asyncio
async def test_employee_photo_foreign_404(db, люди, client_employee):
    """Чужой снимок неотличим от несуществующего чека."""
    await _чек_со_снимком(db, 2, ADMIN_ID)
    свой = await client_employee.get("/api/receipts/999/photo")
    чужой = await client_employee.get("/api/receipts/2/photo")
    assert чужой.status_code == свой.status_code == 404
    assert чужой.json() == свой.json()


@pytest.mark.asyncio
async def test_admin_photo_foreign_ok(db, люди, client):
    """Заведомо разная пара: тот же чек админу отдаётся."""
    await _чек_со_снимком(db, 2, EMP_ID)
    resp = await client.get("/api/receipts/2/photo")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_photo_of_foreign_org_404_even_for_admin(db, люди, client):
    await db.добавить_организацию(id=777, name="Чужая")
    await db.добавить_пользователя(id=9, first_name="Чужой", role="admin", org_id=777)
    await _чек_со_снимком(db, 3, 9, org_id=777)
    resp = await client.get("/api/receipts/3/photo")
    assert resp.status_code == 404
