"""Задача #1: профиль организации (GET/PATCH /api/organizations/me).

GET — любой член орг; PATCH — только admin (_require_admin). org_id берётся из
токена (get_current_user), не из пути → cross-org доступ невозможен. Тип орг в
v1 — read-only. Гоняется на FakePool (таблица organizations в conftest).
client = admin (org_id=1), client_employee = сотрудник (мутации → 403)."""

from datetime import datetime


def _seed_org(db, org_id=1, name="АОЦГ", inn="7707083893", type="company"):
    db.organizations.append(
        dict(
            id=org_id,
            name=name,
            inn=inn,
            type=type,
            owner_id=1,
            created_at=datetime.utcnow(),
        )
    )


# ─── GET /api/organizations/me ───
async def test_get_my_org_returns_profile(client, db):
    _seed_org(db)
    resp = await client.get("/api/organizations/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 1
    assert body["name"] == "АОЦГ"
    assert body["inn"] == "7707083893"
    assert body["type"] == "company"
    assert {"id", "name", "inn", "type", "owner_id", "created_at"} <= set(body)


async def test_get_my_org_404_when_absent(client, db):
    resp = await client.get("/api/organizations/me")
    assert resp.status_code == 404


# ─── PATCH /api/organizations/me ───
async def test_patch_my_org_admin_updates(client, db):
    _seed_org(db)
    resp = await client.patch(
        "/api/organizations/me", json={"name": "АОЦГ Групп", "inn": "7736207543"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "АОЦГ Групп"
    org = next(o for o in db.organizations if o["id"] == 1)
    assert org["name"] == "АОЦГ Групп"
    assert org["inn"] == "7736207543"


async def test_patch_my_org_invalid_inn_422(client, db):
    _seed_org(db)
    resp = await client.patch("/api/organizations/me", json={"inn": "000"})
    assert resp.status_code == 422
    org = next(o for o in db.organizations if o["id"] == 1)
    assert org["inn"] == "7707083893"  # не изменилось


async def test_patch_my_org_partial_keeps_other(client, db):
    _seed_org(db)
    resp = await client.patch("/api/organizations/me", json={"name": "Новое имя"})
    assert resp.status_code == 200
    org = next(o for o in db.organizations if o["id"] == 1)
    assert org["name"] == "Новое имя"
    assert org["inn"] == "7707083893"  # ИНН не трогали → COALESCE сохранил


async def test_patch_my_org_readonly_field_422(client, db):
    _seed_org(db)
    # type — read-only; extra="forbid" → 422, изменение не применяется.
    resp = await client.patch(
        "/api/organizations/me", json={"name": "X", "type": "person"}
    )
    assert resp.status_code == 422
    org = next(o for o in db.organizations if o["id"] == 1)
    assert org["type"] == "company"
    assert org["name"] == "АОЦГ"


async def test_patch_my_org_empty_name_400(client, db):
    _seed_org(db)
    resp = await client.patch("/api/organizations/me", json={"name": "   "})
    assert resp.status_code == 400


async def test_patch_my_org_non_admin_403(client_employee, db):
    _seed_org(db)
    resp = await client_employee.patch(
        "/api/organizations/me", json={"name": "Хочу переименовать"}
    )
    assert resp.status_code == 403
    org = next(o for o in db.organizations if o["id"] == 1)
    assert org["name"] == "АОЦГ"  # не изменилось
