"""Форма ответа GET /api/users/ зависит от роли смотрящего (задача S-28).

ЧТО БЫЛО ДО 07.08.2026. Ручка отдавала `SELECT *` минус два секрета
(`password_hash`, `email_verify_token`) ЛЮБОМУ авторизованному члену
организации. То есть рядовой сотрудник получал не «список имён для фильтра»,
а выгрузку кадровой таблицы: email, ИНН, регион, табельный номер, роль,
флаги — по каждому коллеге. Гейта роли не было, только `org_id`.

ПОЧЕМУ РЕЖЕТСЯ ФОРМА, А НЕ ДОСТУП. Список нужен КАЖДОМУ: на своих же
отчётах подпись автора рисуется по нему (`authorName` в OtchetyPage).
Закрыть ручку сотруднику — сломать подпись; поэтому admin и accountant
получают строку целиком, остальные — id и ФИО.

ЧТО ИМЕННО ПРОВЕРЯЕТСЯ (правило рядом с T11): гейт чтения считается
проверенным, только если тест смотрит на СОДЕРЖИМОЕ ответа, а не на код 200.
Отсюда две половины у каждой проверки: чувствительные поля отсутствуют
И нужные присутствуют — иначе «урезание» до пустого словаря прошло бы
как успех.
"""

import pytest

# Поля, которых у сотрудника быть не должно ни при каких обстоятельствах.
ЧУВСТВИТЕЛЬНЫЕ = ("email", "inn", "region", "employee_id", "role", "is_active")


@pytest.fixture
def кадры(db, seeded):
    """Коллега с заполненной кадровой карточкой — есть чему утекать."""
    db.users.append(
        dict(
            id=3,
            first_name="Мария",
            last_name="Кузнецова",
            patronymic="Ивановна",
            email="maria@example.com",
            role="accountant",
            org_id=1,
            is_active=True,
            password_hash="$2b$12$заглушка",
            inn="123456789012",
            region="77",
            employee_id="ТН-14",
        )
    )
    db._uid = 3
    return db


@pytest.mark.asyncio
async def test_employee_gets_names_only(client_employee, кадры):
    r = await client_employee.get("/api/users/")
    assert r.status_code == 200, r.text
    строки = r.json()
    assert len(строки) == 2, "оба активных сотрудника орг должны быть в списке"

    мария = next(u for u in строки if u["id"] == 3)
    # Половина первая: кадровых полей нет.
    for поле in ЧУВСТВИТЕЛЬНЫЕ:
        assert поле not in мария, f"сотрудник не должен видеть «{поле}» коллеги"
    # Половина вторая: то, ради чего список и грузится, на месте — иначе
    # урезание до пустого словаря прошло бы как успех.
    assert мария["first_name"] == "Мария"
    assert мария["last_name"] == "Кузнецова"
    assert мария["patronymic"] == "Ивановна"
    assert set(мария) == {"id", "first_name", "last_name", "patronymic"}


@pytest.mark.asyncio
async def test_accountant_gets_full_card(client_accountant, кадры):
    """Бухгалтеру форма не режется — иначе «починка» ломает Настройки."""
    r = await client_accountant.get("/api/users/")
    assert r.status_code == 200, r.text
    мария = next(u for u in r.json() if u["id"] == 3)
    assert мария["email"] == "maria@example.com"
    assert мария["inn"] == "123456789012"
    assert мария["role"] == "accountant"


@pytest.mark.asyncio
async def test_admin_gets_full_card(client, кадры):
    r = await client.get("/api/users/")
    assert r.status_code == 200, r.text
    мария = next(u for u in r.json() if u["id"] == 3)
    assert мария["email"] == "maria@example.com"
    assert мария["employee_id"] == "ТН-14"


@pytest.mark.asyncio
async def test_unknown_role_gets_reduced_shape(client, as_role, кадры):
    """Роль вне списка (будущая manager из Финансов) — форма урезанная.

    Умолчание должно быть безопасным: неизвестная роль получает меньше,
    а не всё. Проверяется отдельно от employee, потому что это ДРУГАЯ
    ветка условия — «не входит в белый список», а не «равна employee».
    """
    as_role("manager")
    r = await client.get("/api/users/")
    assert r.status_code == 200, r.text
    мария = next(u for u in r.json() if u["id"] == 3)
    assert "email" not in мария and "inn" not in мария
    assert мария["last_name"] == "Кузнецова"


@pytest.mark.asyncio
async def test_password_hash_never_leaks(client, as_role, кадры):
    """Секреты не отдаются никому — это про _safe, а не про роли.

    Роль меняется через as_role, а не двумя клиентскими фикстурами сразу:
    они обе подменяют get_current_user, и победила бы объявленная последней —
    «оба случая» вышли бы одним и тем же.
    """
    for роль in ("admin", "accountant", "employee"):
        as_role(роль)
        r = await client.get("/api/users/")
        assert r.status_code == 200, r.text
        for u in r.json():
            assert "password_hash" not in u
            assert "email_verify_token" not in u
