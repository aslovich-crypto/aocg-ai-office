"""Регистрация: организация, владелец и справочник — в одной транзакции (S-31).

САМАЯ СЛОЖНАЯ ИЗ НЕПОКРЫТЫХ РУЧЕК, поэтому оставлена напоследок.
`POST /api/auth/register` за один запрос создаёт организацию, её первого
пользователя-администратора, проставляет владельца и засевает справочник
категорий (11 групп + 48 статей) — всё внутри `conn.transaction()`.
До 07.08.2026 не выполнялась в тестах ни разу.

ЧТО ЗДЕСЬ ВАЖНО ПРОВЕРИТЬ, кроме кодов ответа:
  • состав созданного: орг, админ в ней, владелец, справочник;
  • отказ НИЧЕГО не создаёт — иначе половина транзакции остаётся мусором;
  • «подхват» аккаунта без пароля НЕ плодит вторую организацию: так
    заводились сотрудники до появления входа, и их данные должны остаться
    на месте.

ОГОВОРКА ЧЕСТНАЯ: FakePool не откатывает транзакцию по-настоящему —
он снимает снимок и возвращает его при исключении (см. _Txn в conftest).
Поэтому «ничего не создано» здесь означает «хендлер не дошёл до записи»,
а не «PostgreSQL откатил». Настоящая проверка отката — задача T15.
"""

import pytest

import app.routers.auth as auth_router


@pytest.fixture(autouse=True)
def без_почты(monkeypatch):
    """Почта выключена → регистрация сразу выдаёт токены, без ветки письма."""
    monkeypatch.setattr(auth_router, "email_enabled", lambda: False)


ТЕЛО = {
    "email": "Boss@Example.com",
    "password": "парольдлинный",
    "first_name": "Алексей",
    "last_name": "Шукалович",
    "org_name": "ООО Ромашка",
    "inn": "7700000000",
}


@pytest.mark.asyncio
async def test_register_creates_org_admin_owner_and_catalog(client, db):
    r = await client.post("/api/auth/register", json=ТЕЛО)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["access_token"] and d["refresh_token"]

    (орг,) = db.organizations
    assert орг["name"] == "ООО Ромашка" and орг["inn"] == "7700000000"
    assert орг["type"] == "company"

    (юзер,) = db.users
    assert юзер["role"] == "admin", "первый в организации — администратор"
    assert юзер["org_id"] == орг["id"]
    assert юзер["email"] == "boss@example.com", "email приводится к нижнему регистру"
    assert юзер["password_hash"] and юзер["password_hash"] != ТЕЛО["password"]

    assert орг["owner_id"] == юзер["id"], "владелец организации обязан проставиться"
    assert d["organization"]["name"] == "ООО Ромашка"

    # Справочник — часть той же транзакции: без него новая орг не может
    # категоризировать чеки, и это ломается молча.
    свои = [c for c in db.categories if c.get("org_id") == орг["id"]]
    группы = [g for g in db.category_groups if g.get("org_id") == орг["id"]]
    assert len(группы) >= 10 and len(свои) >= 40, (
        f"справочник не засеян: групп {len(группы)}, статей {len(свои)}"
    )


@pytest.mark.asyncio
async def test_register_without_org_name_falls_back_to_person(client, db):
    """Имя организации не обязательно — подставляется ФИО, тип из тела."""
    r = await client.post(
        "/api/auth/register",
        json={
            "email": "ip@example.com",
            "password": "парольдлинный",
            "first_name": "Иван",
            "last_name": "Петров",
            "org_type": "person",
        },
    )
    assert r.status_code == 200, r.text
    (орг,) = db.organizations
    assert орг["name"] == "Иван Петров" and орг["type"] == "person"


@pytest.mark.asyncio
async def test_taken_email_creates_nothing(client, db):
    db.organizations.append(dict(id=1, name="Чужая", inn=None, type="company"))
    db.users.append(
        dict(
            id=1,
            email="boss@example.com",
            password_hash="уже-есть",
            role="admin",
            org_id=1,
            is_active=True,
            is_email_verified=True,
        )
    )
    r = await client.post("/api/auth/register", json=ТЕЛО)
    assert r.status_code == 409, r.text
    assert "access_token" not in r.json()
    assert len(db.organizations) == 1, "вторая организация не должна появиться"
    assert len(db.users) == 1


@pytest.mark.asyncio
async def test_short_password_creates_nothing(client, db):
    r = await client.post("/api/auth/register", json={**ТЕЛО, "password": "1234"})
    assert r.status_code == 400, r.text
    assert db.organizations == [] and db.users == []
    assert db.categories == [], "справочник тоже не должен появиться"


@pytest.mark.asyncio
async def test_broken_email_creates_nothing(client, db):
    r = await client.post("/api/auth/register", json={**ТЕЛО, "email": "без-собаки"})
    assert r.status_code == 400, r.text
    assert db.organizations == [] and db.users == []


@pytest.mark.asyncio
async def test_seeded_account_is_claimed_not_duplicated(client, db):
    """Аккаунт без пароля (заведён до входа) ПОДХВАТЫВАЕТСЯ, а не дублируется.

    Его организация и данные остаются его же — вторая орг здесь была бы
    молчаливой потерей всех чеков сотрудника.
    """
    db.organizations.append(dict(id=1, name="ООО Ромашка", inn=None, type="company"))
    db.users.append(
        dict(
            id=3,
            first_name="Иван",
            last_name="Петров",
            email="boss@example.com",
            phone=None,
            password_hash=None,
            role="employee",
            org_id=1,
            is_active=True,
            is_email_verified=False,
            email_verify_token=None,
        )
    )
    db._uid = 3

    r = await client.post("/api/auth/register", json=ТЕЛО)
    assert r.status_code == 200, r.text
    assert len(db.organizations) == 1, "новая организация НЕ создаётся"
    assert len(db.users) == 1, "новый пользователь НЕ создаётся"

    (юзер,) = db.users
    assert юзер["id"] == 3 and юзер["org_id"] == 1
    assert юзер["password_hash"], "пароль обязан проставиться"
    assert юзер["role"] == "employee", "подхват не повышает роль до админа"
    assert юзер["first_name"] == "Алексей", "имя из формы перекрывает старое"
