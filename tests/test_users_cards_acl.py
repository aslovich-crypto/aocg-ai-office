"""Ролевые гейты на МУТАЦИИ справочников людей и карт (задача S-29).

ЧТО БЫЛО ДО 07.08.2026. В `app/routers/users.py` и `app/routers/cards.py`
не было ни одной проверки роли — только `org_id` в WHERE. Значит любой
авторизованный сотрудник мог:
  • завести пользователя (роль приходит ИЗ ТЕЛА запроса — то есть завести
    себе администратора);
  • переписать коллеге ФИО, email, ИНН, регион, табельный номер;
  • ОТКЛЮЧИТЬ кого угодно, включая администратора;
  • переименовать, удалить и переназначить корпоративную карту организации.
Тестами это не покрывалось вовсе: в FakePool не было даже таблицы users —
запросы к ней никто никогда не выполнял.

РАЗНЫЙ КРУГ ДЛЯ РАЗНЫХ СПРАВОЧНИКОВ, И ЭТО НЕ ПРОИЗВОЛ:
  • users — ТОЛЬКО admin. Создание пользователя выдаёт доступ к данным
    организации, PATCH правит чужие персональные данные, DELETE отбирает
    доступ. Свой профиль правится через PATCH /me, он без гейта.
  • cards — admin ИЛИ accountant, тот же круг, что у справочника категорий
    (`_require_category_manager`). Карты это бухгалтерский справочник
    способов оплаты; удаление карты не выдаёт доступ и не трогает ПД.

GET в обоих случаях остаётся открытым: карты нужны сотруднику для ввода
чека, список людей — тема отдельной задачи S-28 (там про утечку ЧТЕНИЯ).
"""

import pytest


# ─────────────────────────── users: только admin ───────────────────────────


@pytest.mark.asyncio
async def test_employee_cannot_create_user(client_employee, db, seeded):
    r = await client_employee.post(
        "/api/users/",
        json={"first_name": "Пётр", "last_name": "Сидоров", "role": "admin"},
    )
    assert r.status_code == 403, r.text
    assert len(db.users) == 1, "пользователь не должен был создаться"


@pytest.mark.asyncio
async def test_accountant_cannot_create_user(client_accountant, db, seeded):
    r = await client_accountant.post(
        "/api/users/", json={"first_name": "Пётр", "last_name": "Сидоров"}
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_employee_cannot_patch_colleague(client_employee, db, seeded):
    r = await client_employee.patch("/api/users/2", json={"inn": "123456789012"})
    assert r.status_code == 403, r.text
    assert db.users[0].get("inn") is None, "ИНН коллеги не должен был измениться"


@pytest.mark.asyncio
async def test_employee_cannot_deactivate_colleague(client_employee, db, seeded):
    r = await client_employee.delete("/api/users/2")
    assert r.status_code == 403, r.text
    assert db.users[0]["is_active"] is True, "коллега не должен был отключиться"


@pytest.mark.asyncio
async def test_accountant_cannot_deactivate_user(client_accountant, db, seeded):
    r = await client_accountant.delete("/api/users/2")
    assert r.status_code == 403, r.text
    assert db.users[0]["is_active"] is True


@pytest.mark.asyncio
async def test_admin_still_manages_users(client, db, seeded):
    """Админ работает как раньше — иначе гейт «чинит» ценой поломки."""
    created = await client.post(
        "/api/users/", json={"first_name": "Пётр", "last_name": "Сидоров"}
    )
    assert created.status_code == 200, created.text
    assert created.json()["first_name"] == "Пётр"

    patched = await client.patch("/api/users/2", json={"inn": "123456789012"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["inn"] == "123456789012"

    gone = await client.delete("/api/users/2")
    assert gone.status_code == 200, gone.text
    assert db.users[0]["is_active"] is False


# ────────────────── cards: admin или accountant (как категории) ─────────────


@pytest.mark.asyncio
async def test_employee_cannot_touch_cards(client_employee, db, seeded):
    было = len(db.cards)
    assert (
        await client_employee.post("/api/cards/", json={"name": "Моя"})
    ).status_code == 403
    assert (
        await client_employee.patch("/api/cards/1", json={"name": "Переименована"})
    ).status_code == 403
    assert (await client_employee.patch("/api/cards/1/default")).status_code == 403
    assert (await client_employee.delete("/api/cards/1")).status_code == 403
    assert len(db.cards) == было
    assert db.cards[0]["name"] == "Корп.карта", "карта не должна была измениться"


@pytest.mark.asyncio
async def test_employee_still_reads_cards(client_employee):
    """GET остаётся открытым: без списка карт сотрудник не введёт чек."""
    r = await client_employee.get("/api/cards/")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_accountant_manages_cards(client_accountant, db, seeded):
    r = await client_accountant.post("/api/cards/", json={"name": "Бухгалтерская"})
    assert r.status_code == 200, r.text
    assert any(c["name"] == "Бухгалтерская" for c in db.cards)


@pytest.mark.asyncio
async def test_admin_manages_cards(client, db, seeded):
    assert (
        await client.patch("/api/cards/1", json={"name": "Новая"})
    ).status_code == 200
    assert (await client.patch("/api/cards/1/default")).status_code == 200
    assert db.cards[0]["is_default"] is True
    assert (await client.delete("/api/cards/1")).status_code == 200
    assert all(c["id"] != 1 for c in db.cards)
