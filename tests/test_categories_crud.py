"""Фикс №1 фаза C: CRUD справочника категорий.

GET — любой член орг; POST/PATCH/DELETE/visibility — admin/accountant (гейт
_require_category_manager). Всё org-scoped. Гоняется на FakePool со справочником,
засеянным seed_default_categories(db, org). client = admin, client_accountant,
client_employee — фикстуры с разной ролью (conftest)."""
from app.categories_seed import seed_default_categories


def _group_id(db, org_id=1):
    return next(g["id"] for g in db.category_groups if g.get("org_id") == org_id)


def _default_cat_id(db, org_id=1):
    return next(c["id"] for c in db.categories if c.get("org_id") == org_id and c.get("is_default"))


# ─── GET /api/categories ───
async def test_get_categories_nested_structure(client, db):
    await seed_default_categories(db, 1)
    resp = await client.get("/api/categories/")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["groups"]) == 11
    assert sum(len(g["categories"]) for g in body["groups"]) == 48
    sample = body["groups"][0]["categories"][0]
    assert {"id", "name", "tax_kind", "position", "is_default", "is_visible"} <= set(sample)
    assert sample["is_default"] is True


async def test_get_categories_org_isolation(client, db):
    await seed_default_categories(db, 1)
    await seed_default_categories(db, 2)
    body = (await client.get("/api/categories/")).json()  # client = org_id=1
    ids = {c["id"] for g in body["groups"] for c in g["categories"]}
    assert ids == {c["id"] for c in db.categories if c["org_id"] == 1}
    assert len(ids) == 48


async def test_get_categories_visible_only_filter(client, db):
    await seed_default_categories(db, 1)
    hidden = _default_cat_id(db)
    next(c for c in db.categories if c["id"] == hidden)["is_visible"] = False
    full = (await client.get("/api/categories/")).json()
    assert any(c["id"] == hidden for g in full["groups"] for c in g["categories"])
    vis = (await client.get("/api/categories/?visible_only=true")).json()
    assert not any(c["id"] == hidden for g in vis["groups"] for c in g["categories"])


async def test_get_categories_as_employee_ok(client_employee, db):
    await seed_default_categories(db, 1)
    resp = await client_employee.get("/api/categories/")
    assert resp.status_code == 200
    assert len(resp.json()["groups"]) == 11


# ─── POST /api/categories — ролевой гейт ───
async def test_category_create_as_admin_ok(client, db):
    await seed_default_categories(db, 1)
    resp = await client.post("/api/categories/", json={"name": "Моя статья", "group_id": _group_id(db)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_default"] is False
    assert body["tax_kind"] == "Прочие расходы"  # дефолт Q4


async def test_category_create_as_accountant_ok(client_accountant, db):
    await seed_default_categories(db, 1)
    resp = await client_accountant.post(
        "/api/categories/", json={"name": "Статья бухгалтера", "group_id": _group_id(db)})
    assert resp.status_code == 200
    assert resp.json()["is_default"] is False


async def test_category_create_as_employee_403(client_employee, db):
    await seed_default_categories(db, 1)
    resp = await client_employee.post(
        "/api/categories/", json={"name": "Нельзя", "group_id": _group_id(db)})
    assert resp.status_code == 403


# ─── POST — валидации ───
async def test_category_create_duplicate_409(client, db):
    await seed_default_categories(db, 1)
    resp = await client.post("/api/categories/", json={"name": "Топливо", "group_id": _group_id(db)})
    assert resp.status_code == 409


async def test_category_create_foreign_group_404(client, db):
    await seed_default_categories(db, 1)
    await seed_default_categories(db, 2)
    foreign = next(g["id"] for g in db.category_groups if g["org_id"] == 2)
    resp = await client.post("/api/categories/", json={"name": "Чужая группа", "group_id": foreign})
    assert resp.status_code == 404


async def test_category_create_invalid_tax_kind_400(client, db):
    await seed_default_categories(db, 1)
    resp = await client.post("/api/categories/", json={
        "name": "Плохой вид", "group_id": _group_id(db), "tax_kind": "Чепуха"})
    assert resp.status_code == 400


async def test_category_create_explicit_tax_kind(client, db):
    await seed_default_categories(db, 1)
    resp = await client.post("/api/categories/", json={
        "name": "Материальная", "group_id": _group_id(db), "tax_kind": "Материальные расходы"})
    assert resp.status_code == 200
    assert resp.json()["tax_kind"] == "Материальные расходы"


# ─── PATCH /api/categories/{id} ───
async def test_category_patch_rename_and_tax_kind_ok(client, db):
    await seed_default_categories(db, 1)
    cid = (await client.post(
        "/api/categories/", json={"name": "Старое", "group_id": _group_id(db)})).json()["id"]
    resp = await client.patch(f"/api/categories/{cid}", json={
        "name": "Новое", "tax_kind": "Материальные расходы"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Новое"
    assert body["tax_kind"] == "Материальные расходы"


async def test_category_patch_system_403(client, db):
    await seed_default_categories(db, 1)
    resp = await client.patch(
        f"/api/categories/{_default_cat_id(db)}", json={"name": "Переименовать системную"})
    assert resp.status_code == 403


async def test_category_patch_not_found_404(client, db):
    await seed_default_categories(db, 1)
    resp = await client.patch("/api/categories/999999", json={"name": "Нет такой"})
    assert resp.status_code == 404


async def test_category_patch_invalid_tax_kind_400(client, db):
    await seed_default_categories(db, 1)
    cid = (await client.post(
        "/api/categories/", json={"name": "Для правки", "group_id": _group_id(db)})).json()["id"]
    resp = await client.patch(f"/api/categories/{cid}", json={"tax_kind": "Несуществующий"})
    assert resp.status_code == 400


# ─── DELETE /api/categories/{id} ───
async def test_category_delete_ok(client, db):
    await seed_default_categories(db, 1)
    cid = (await client.post(
        "/api/categories/", json={"name": "На удаление", "group_id": _group_id(db)})).json()["id"]
    resp = await client.delete(f"/api/categories/{cid}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert all(c["id"] != cid for c in db.categories)


async def test_category_delete_system_403(client, db):
    await seed_default_categories(db, 1)
    resp = await client.delete(f"/api/categories/{_default_cat_id(db)}")
    assert resp.status_code == 403


async def test_category_delete_with_receipts_409(client, db):
    await seed_default_categories(db, 1)
    cid = (await client.post(
        "/api/categories/", json={"name": "С чеками", "group_id": _group_id(db)})).json()["id"]
    db.receipts.append({"id": 1, "org_id": 1, "category_id": cid})
    resp = await client.delete(f"/api/categories/{cid}")
    assert resp.status_code == 409
    assert "1" in resp.json()["detail"]


# ─── PATCH /api/categories/{id}/visibility ───
async def test_category_visibility_hide_show_system(client, db):
    await seed_default_categories(db, 1)
    cid = _default_cat_id(db)
    hide = await client.patch(f"/api/categories/{cid}/visibility", json={"is_visible": False})
    assert hide.status_code == 200
    assert hide.json()["is_visible"] is False
    show = await client.patch(f"/api/categories/{cid}/visibility", json={"is_visible": True})
    assert show.json()["is_visible"] is True


async def test_category_visibility_with_receipts_allowed(client, db):
    await seed_default_categories(db, 1)
    cid = _default_cat_id(db)
    db.receipts.append({"id": 1, "org_id": 1, "category_id": cid})
    resp = await client.patch(f"/api/categories/{cid}/visibility", json={"is_visible": False})
    assert resp.status_code == 200  # скрыть ≠ удалить (Q6)


async def test_category_visibility_as_employee_403(client_employee, db):
    await seed_default_categories(db, 1)
    resp = await client_employee.patch(
        f"/api/categories/{_default_cat_id(db)}/visibility", json={"is_visible": False})
    assert resp.status_code == 403
