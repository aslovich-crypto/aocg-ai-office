"""A-ACL: разграничение доступа к чекам по автору и роли.

Модель:
- VIEW + PATCH: employee — только свои; accountant/admin — все в орг.
- DELETE/bulk: employee/accountant — только свои; admin — любые в орг.
- dedupe-cleanup — только admin.
Фикстуры (conftest): client=admin(id=1), client_accountant(id=1), client_employee(id=2).
"""

from datetime import date, datetime

import pytest

ADMIN_ID = 1  # client (admin) и client_accountant используют user_id=1
EMP_ID = 2  # client_employee — user_id=2
ORG = 1


def _seed(db, rid, user_id, org_id=ORG):
    """Кладёт чек прямо в FakePool с заданным автором (минимум полей для эндпоинтов)."""
    db.receipts.append(
        {
            "id": rid,
            "org_id": org_id,
            "user_id": user_id,
            "date": date(2026, 6, 1),
            "amount": 100,
            "org": "ООО Тест",
            "payment": "Карта",
            "kkt_fn": None,
            "fd_num": None,
            "raw_data": None,
            "photo_url": None,
            "created_at": datetime.utcnow(),
        }
    )


# ── VIEW ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_employee_listing_sees_only_own(db, client_employee):
    _seed(db, 101, ADMIN_ID)  # чужой
    _seed(db, 102, EMP_ID)  # свой
    r = await client_employee.get("/api/receipts/")
    assert r.status_code == 200
    assert {x["id"] for x in r.json()} == {102}


@pytest.mark.asyncio
async def test_accountant_listing_sees_all(db, client_accountant):
    _seed(db, 101, ADMIN_ID)
    _seed(db, 102, EMP_ID)
    r = await client_accountant.get("/api/receipts/")
    assert {x["id"] for x in r.json()} == {101, 102}


@pytest.mark.asyncio
async def test_admin_listing_sees_all(db, client):
    _seed(db, 101, ADMIN_ID)
    _seed(db, 102, EMP_ID)
    r = await client.get("/api/receipts/")
    assert {x["id"] for x in r.json()} == {101, 102}


@pytest.mark.asyncio
async def test_employee_get_foreign_404_own_200(db, client_employee):
    _seed(db, 101, ADMIN_ID)  # чужой
    _seed(db, 102, EMP_ID)  # свой
    assert (await client_employee.get("/api/receipts/101")).status_code == 404
    assert (await client_employee.get("/api/receipts/102")).status_code == 200


# ── PATCH ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_employee_patch_foreign_404(db, client_employee):
    _seed(db, 101, ADMIN_ID)  # чужой
    r = await client_employee.patch("/api/receipts/101", json={"payment": "Нал"})
    assert r.status_code == 404
    assert db.receipts[0]["payment"] == "Карта"  # не изменён


@pytest.mark.asyncio
async def test_accountant_patch_foreign_ok(db, client_accountant):
    _seed(db, 102, EMP_ID)  # автор employee — правит accountant
    r = await client_accountant.patch("/api/receipts/102", json={"payment": "Нал"})
    assert r.status_code == 200
    assert db.receipts[0]["payment"] == "Нал"


# ── DELETE ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_employee_delete_foreign_noop(db, client_employee):
    _seed(db, 101, ADMIN_ID)  # чужой
    r = await client_employee.delete("/api/receipts/101")
    assert r.status_code == 200  # anti-enum: всегда 200
    assert any(x["id"] == 101 for x in db.receipts)  # но не удалён


@pytest.mark.asyncio
async def test_accountant_delete_foreign_noop(db, client_accountant):
    _seed(db, 102, EMP_ID)  # автор employee
    r = await client_accountant.delete("/api/receipts/102")
    assert r.status_code == 200
    assert any(x["id"] == 102 for x in db.receipts)  # accountant НЕ удаляет чужой


@pytest.mark.asyncio
async def test_admin_delete_foreign_ok(db, client):
    _seed(db, 102, EMP_ID)  # автор employee — удаляет admin
    r = await client.delete("/api/receipts/102")
    assert r.status_code == 200
    assert not any(x["id"] == 102 for x in db.receipts)  # удалён


# ── bulk-delete + dedupe-cleanup ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_bulk_delete_employee_ignores_foreign(db, client_employee):
    _seed(db, 101, ADMIN_ID)  # чужой
    _seed(db, 102, EMP_ID)  # свой
    r = await client_employee.post(
        "/api/receipts/bulk-delete", json={"ids": [101, 102]}
    )
    assert r.status_code == 200
    assert r.json()["deleted"] == [102]
    remaining = {x["id"] for x in db.receipts}
    assert 101 in remaining and 102 not in remaining


@pytest.mark.asyncio
async def test_dedupe_cleanup_403_for_non_admin(db, client_employee):
    r = await client_employee.post("/api/receipts/dedupe-cleanup/")
    assert r.status_code == 403
