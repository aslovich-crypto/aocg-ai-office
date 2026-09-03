# -*- coding: utf-8 -*-
"""Ролевые гейты на МУТАЦИИ справочников людей и карт (S-29) — НА ЖИВОЙ БАЗЕ.

ПЕРЕВЕДЕНО С FakePool 03.09.2026 (T36, пункт 2, первая сюита). Тесты те же,
прибор другой: SQL роутеров исполняется настоящим PostgreSQL, а не толкуется
двойником. Повод именно для этой сюиты: зеркало счёта админов в двойнике
держало `is_active` в ветке намертво — мутация «снять `AND is_active=true`
из счёта оставшихся админов» проходила на двойнике ЗЕЛЁНОЙ, а в проде
открывала снятие последнего активного админа при живом погашенном.
Здесь та же мутация краснеет (замер при переводе — в сообщении коммита).

ЧТО БЫЛО ДО 07.08.2026. В `app/routers/users.py` и `app/routers/cards.py`
не было ни одной проверки роли — только `org_id` в WHERE. Значит любой
авторизованный сотрудник мог:
  • завести пользователя (роль приходит ИЗ ТЕЛА запроса — то есть завести
    себе администратора);
  • переписать коллеге ФИО, email, ИНН, регион, табельный номер;
  • ОТКЛЮЧИТЬ кого угодно, включая администратора;
  • переименовать, удалить и переназначить корпоративную карту организации.

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
async def test_вторая_дверь_к_заведению_людей_ЗАКРЫТА(client, db, seeded):
    """⚠️ T104, ЭТАП ④: `POST /api/users/` снесена, и это надо стеречь.

    Она заводила человека БЕЗ ПАРОЛЯ и без письма: строка в `users` появлялась,
    а войти по ней было нельзя. Именно так 30.08.2026 «завёлся» Анис Ламри,
    которого потом не нашли ни в списке, ни в базе по его почте.

    ⚠️ Без этого теста снос ничем не охраняется: ручку вернут «на всякий
    случай», и мина встанет обратно молча. Проверяется ПОВЕДЕНИЕ ручки,
    а не отсутствие строки в исходнике.
    """
    r = await client.post(
        "/api/users/", json={"first_name": "Пётр", "last_name": "Сидоров"}
    )
    assert r.status_code in (404, 405), (
        f"POST /api/users/ снова отвечает {r.status_code} — "
        "вторая дверь к заведению людей открылась"
    )
    assert await db.число_пользователей() == 1, "и строки завестись не должно"


@pytest.mark.asyncio
async def test_employee_cannot_invite(client_employee, db, seeded):
    # ⚠️ ПЕРЕВЕДЕНО НА ПРИГЛАШЕНИЕ 31.08.2026 (T104, этап ④): `POST /api/users/`
    # снесена, единственный путь завести человека — `POST /api/invite/create`.
    # Тест НЕ УДАЛЁН, а переведён: гейт тот же, дверь другая.
    r = await client_employee.post(
        "/api/invite/create",
        json={"role": "admin", "email": "p@example.com"},
    )
    assert r.status_code == 403, r.text
    assert await db.число_пользователей() == 1, "пользователь не должен был создаться"
    assert await db.число_приглашений() == 0, "и приглашение выписаться не должно"


@pytest.mark.asyncio
async def test_accountant_cannot_invite(client_accountant, db, seeded):
    # ⚠️ Та же дверь, что у сотрудника (T104, этап ④).
    r = await client_accountant.post(
        "/api/invite/create", json={"role": "employee", "email": "p@example.com"}
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_employee_cannot_patch_colleague(client_employee, db, seeded):
    r = await client_employee.patch("/api/users/2", json={"inn": "123456789012"})
    assert r.status_code == 403, r.text
    assert (await db.пользователь(2))["inn"] is None, (
        "ИНН коллеги не должен был измениться"
    )


@pytest.mark.asyncio
async def test_employee_cannot_deactivate_colleague(client_employee, db, seeded):
    r = await client_employee.delete("/api/users/2")
    assert r.status_code == 403, r.text
    assert (await db.пользователь(2))["is_active"] is True, (
        "коллега не должен был отключиться"
    )


@pytest.mark.asyncio
async def test_accountant_cannot_deactivate_user(client_accountant, db, seeded):
    r = await client_accountant.delete("/api/users/2")
    assert r.status_code == 403, r.text
    assert (await db.пользователь(2))["is_active"] is True


@pytest.mark.asyncio
async def test_админ_не_может_отключить_самого_себя(client, db, seeded):
    """Снявший себя не отменит своё же действие — доступа у него уже нет.

    ⚠️ ДЫРА БЫЛА ЖИВОЙ (T119): `deactivate_user` проверял только роль
    просящего и организацию. Админ мог отключить СЕБЯ, и организация
    оставалась неуправляемой — ни завести сотрудника, ни создать
    приглашение, ни вернуть себе роль. После T115 отключённый не может
    даже войти, чтобы попробовать: тупик без выхода.
    """
    await db.добавить_пользователя(id=1, first_name="Админ", role="admin")
    r = await client.delete("/api/users/1")
    assert r.status_code == 409, r.text
    # ⚠️ ТЕКСТ ПРОВЕРЯЕТСЯ ДОСЛОВНО, И ЭТО ТРЕБОВАНИЕ ВЛАДЕЛЬЦА 31.08.2026.
    # До этого дня первой срабатывала проверка «нельзя себя», и человек читал,
    # ЧТО нельзя, — ни почему, ни что делать. Формулировка с причиной
    # существовала, но стояла второй и не показывалась НИКОГДА.
    текст = r.json()["detail"]
    assert "единственный администратор" in текст, текст
    assert "пригласите второго" in текст, "нет следующего шага"
    assert (await db.пользователь(1))["is_active"] is True, (
        "строка не должна была погаснуть"
    )


@pytest.mark.asyncio
async def test_нельзя_снять_последнего_активного_админа(client, db, seeded):
    """Организация без администратора — тупик, чинится только руками в базе.

    ⚠️ ЧЕСТНО О ДОСТИЖИМОСТИ: пока просящий сам активный админ СО СТРОКОЙ
    в таблице, эта ветка не срабатывает — его собственная строка и есть
    «ещё один админ», а себя он снять не может по проверке выше. Здесь
    строки просящего в таблице НЕТ (авторизация подменена зависимостью):
    так выражается край, ради которого защита и написана, и так она
    переиспользуется сменой роли (T104), где понижение админа снимает
    администратора другим путём.
    """
    await db.добавить_пользователя(id=3, first_name="Единственный", role="admin")
    r = await client.delete("/api/users/3")
    assert r.status_code == 409, r.text
    assert "последний администратор" in r.json()["detail"]
    assert (await db.пользователь(3))["is_active"] is True


@pytest.mark.asyncio
async def test_роль_сотрудника_меняется_и_человек_остаётся_собой(client, db, seeded):
    """T118: повышение НЕ должно требовать удаления и заведения заново.

    ⚠️ ЗАЧЕМ ИМЕННО ТАК. До 31.08.2026 роль не входила ни в `UserUpdate`,
    ни в `UPDATABLE` — PATCH молча её отбрасывал и отвечал 400 «No fields
    to update». Единственным способом повысить человека было удалить строку
    и завести заново, то есть **потерять его чеки и отчёты**. Довод
    владельца: приглашают один раз, а роли меняют постоянно.
    """
    r = await client.patch("/api/users/2", json={"role": "accountant"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "accountant"
    строка = await db.пользователь(2)
    assert строка["role"] == "accountant"
    assert строка["id"] == 2, "строка та же — человек не заведён заново"


@pytest.mark.asyncio
async def test_роль_вне_белого_списка_не_проходит(client, db, seeded):
    """Тот же белый список, что у приглашения (S-24): иначе роль есть, а прав нет."""
    r = await client.patch("/api/users/2", json={"role": "manager"})
    assert r.status_code == 422, r.text
    assert (await db.пользователь(2))["role"] != "manager"


@pytest.mark.asyncio
async def test_нельзя_понизить_САМОГО_СЕБЯ(client, db, seeded):
    """Понижение себя — тот же тупик, что отключение себя: отменить некому."""
    await db.добавить_пользователя(id=1, first_name="Админ", role="admin")
    r = await client.patch("/api/users/1", json={"role": "employee"})
    assert r.status_code == 409, r.text
    assert "единственный администратор" in r.json()["detail"]


@pytest.mark.asyncio
async def test_нельзя_понизить_ПОСЛЕДНЕГО_админа(client, db, seeded):
    """⚠️ ПОНИЖЕНИЕ АДМИНА = СНЯТИЕ АДМИНИСТРАТОРА, ТОЛЬКО ДРУГОЙ ДВЕРЬЮ.

    Закрыть DELETE и оставить открытым PATCH значило бы починить одну дверь
    из двух: организация так же осталась бы без администратора.
    """
    await db.добавить_пользователя(id=3, first_name="Единственный", role="admin")
    r = await client.patch("/api/users/3", json={"role": "employee"})
    assert r.status_code == 409, r.text
    assert "последний администратор" in r.json()["detail"]
    assert (await db.пользователь(3))["role"] == "admin"


@pytest.mark.asyncio
async def test_при_двух_админах_понизить_одного_МОЖНО(client, db, seeded):
    """Обратная сторона: запрет, срабатывающий всегда, ломает обычную работу."""
    await db.добавить_пользователя(id=3, first_name="Первый", role="admin")
    await db.добавить_пользователя(id=4, first_name="Второй", role="admin")
    r = await client.patch("/api/users/3", json={"role": "employee"})
    assert r.status_code == 200, r.text
    assert (await db.пользователь(3))["role"] == "employee"
    assert (await db.пользователь(4))["role"] == "admin"


@pytest.mark.asyncio
async def test_повышение_ДО_админа_гейтом_не_блокируется(client, db, seeded):
    """Повышение админов не убавляет — гейт обязан пропускать."""
    r = await client.patch("/api/users/2", json={"role": "admin"})
    assert r.status_code == 200, r.text
    assert (await db.пользователь(2))["role"] == "admin"


@pytest.mark.asyncio
async def test_при_ДВУХ_админах_одного_снять_МОЖНО(client, db, seeded):
    """⚠️ ОБРАТНАЯ СТОРОНА ЗАЩИТЫ T119, И БЕЗ НЕЁ ЗАЩИТА НЕ ДОКАЗАНА.

    Замер 31.08.2026 по требованию владельца: он заводит ВТОРОГО
    администратора, потому что один админ — единая точка отказа на живых
    людях (потерял телефон или почту — организацию некому вести). Значит
    проверка обязана СЧИТАТЬ админов, а не запрещать снятие любого из них:
    запрет «всегда» выглядел бы рабочим на прежнем тесте и заблокировал бы
    ровно тот случай, ради которого второго админа и заводят.
    """
    await db.добавить_пользователя(id=3, first_name="Первый", role="admin")
    await db.добавить_пользователя(id=4, first_name="Второй", role="admin")
    r = await client.delete("/api/users/3")
    assert r.status_code == 200, r.text
    assert (await db.пользователь(3))["is_active"] is False, (
        "при двух админах одного снять можно"
    )
    assert (await db.пользователь(4))["is_active"] is True, (
        "второй админ обязан остаться"
    )


@pytest.mark.asyncio
async def test_снять_ВТОРОГО_из_двух_уже_нельзя(client, db, seeded):
    """И граница: сняв одного из двух, второго снять уже не дают.

    ⚠️ РОВНО ЗДЕСЬ ДВОЙНИК ВРАЛ: его зеркало счёта админов фильтровало
    погашенных само, независимо от SQL. Снятое из роутера `is_active=true`
    оставляло тест на FakePool зелёным — а этот краснеет.
    """
    await db.добавить_пользователя(
        id=3, first_name="Первый", role="admin", is_active=False
    )
    await db.добавить_пользователя(id=4, first_name="Второй", role="admin")
    r = await client.delete("/api/users/4")
    assert r.status_code == 409, r.text
    assert "последний администратор" in r.json()["detail"]
    assert (await db.пользователь(4))["is_active"] is True


@pytest.mark.asyncio
async def test_погашенного_ВИДНО_в_списке_и_он_помечен(client, db, seeded):
    """⚠️ T118/④: до 31.08.2026 погашенный ИСЧЕЗАЛ С ЭКРАНА целиком.

    Свайп срабатывает легко, отменить нельзя было ничем, а увидеть, кого
    погасил, — тоже нельзя: список читал `WHERE is_active = true`. Это не
    «нет кнопки возврата», это потеря наблюдаемости — ошибку не видно даже
    в ту же секунду. Довод владельца: защита от собственной ошибки.
    """
    # ⚠️ Строки самого просящего в таблице нет (авторизация подменена
    # зависимостью — как в тесте про последнего админа), поэтому активного
    # коллегу заводим явно: без него «активные идут первыми» проверять не на чем.
    await db.добавить_пользователя(id=3, first_name="Активный", role="employee")
    await client.delete("/api/users/2")
    r = await client.get("/api/users/")
    assert r.status_code == 200, r.text
    строки = {u["id"]: u for u in r.json()}
    assert 2 in строки, "погашенный обязан остаться виден управляющему"
    assert строки[2]["is_active"] is False, "и быть помечен как отключённый"
    assert строки[3]["is_active"] is True
    assert [u["id"] for u in r.json()] == [3, 2], "активные идут первыми"


@pytest.mark.asyncio
async def test_сотрудник_погашенных_НЕ_видит(client_employee, db, seeded):
    """Обратная сторона: расширять выдачу ВСЕМ значило бы менять поведение
    там, где не просили. У сотрудника управляющего экрана нет, а `users`
    кормит подстановку имён — ответ обязан остаться прежним."""
    await db.погасить(2)
    r = await client_employee.get("/api/users/")
    assert r.status_code == 200, r.text
    assert all(u["id"] != 2 for u in r.json()), "сотруднику погашенные не видны"


@pytest.mark.asyncio
async def test_погашенного_можно_ВЕРНУТЬ(client, db, seeded):
    """Обратное действие, которого не было ни одного: ни ручки, ни поля."""
    await client.delete("/api/users/2")
    assert (await db.пользователь(2))["is_active"] is False
    r = await client.post("/api/users/2/restore")
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is True
    строка = await db.пользователь(2)
    assert строка["is_active"] is True, "человек вернулся в строй"
    assert строка["id"] == 2, "та же строка — чеки и отчёты при нём"


@pytest.mark.asyncio
async def test_вернуть_чужого_нельзя(client, db, seeded):
    """org-scope: возврат не должен быть дырой в чужую организацию."""
    await db.добавить_пользователя(
        id=99, first_name="Чужой", role="employee", org_id=777, is_active=False
    )
    r = await client.post("/api/users/99/restore")
    assert r.status_code == 404, r.text
    assert (await db.пользователь(99))["is_active"] is False


@pytest.mark.asyncio
async def test_возврат_только_админу(client_employee, db, seeded):
    """Сотрудник не возвращает никого — иначе гашение обратимо кем угодно."""
    await db.погасить(2)
    r = await client_employee.post("/api/users/2/restore")
    assert r.status_code == 403, r.text
    assert (await db.пользователь(2))["is_active"] is False


@pytest.mark.asyncio
async def test_себя_отключить_МОЖНО_когда_есть_второй_админ(client, db, seeded):
    """⚠️ ПОРЯДОК ③, РЕШЕНИЕ ВЛАДЕЛЬЦА 31.08.2026: «передать дела и уйти».

    Прежний безусловный запрет был верен ровно при одном администраторе:
    снявший себя не может отменить своё же действие. **При двух отменить
    может второй**, и запрет только мешал: уйти было нельзя вообще, снять
    вас мог лишь кто-то другой.
    """
    await db.добавить_пользователя(id=1, first_name="Ухожу", role="admin")
    await db.добавить_пользователя(id=4, first_name="Остаюсь", role="admin")
    r = await client.delete("/api/users/1")
    assert r.status_code == 200, r.text
    assert (await db.пользователь(1))["is_active"] is False
    assert (await db.пользователь(4))["is_active"] is True


@pytest.mark.asyncio
async def test_себя_понизить_МОЖНО_когда_есть_второй_админ(client, db, seeded):
    """Та же дверь, другой путь: понижение себя — тоже уход из администраторов."""
    await db.добавить_пользователя(id=1, first_name="Ухожу", role="admin")
    await db.добавить_пользователя(id=4, first_name="Остаюсь", role="admin")
    r = await client.patch("/api/users/1", json={"role": "employee"})
    assert r.status_code == 200, r.text
    assert (await db.пользователь(1))["role"] == "employee"


@pytest.mark.asyncio
async def test_отказ_ЧУЖОЙ_строке_объясняет_по_своему(client, db, seeded):
    """⚠️ ДВА ТЕКСТА, ПОТОМУ ЧТО РАЗНЫЙ СЛЕДУЮЩИЙ ШАГ: себе — «пригласите
    второго», чужому — «назначьте другого». Один текст на оба случая отправлял
    бы половину читателей не туда."""
    await db.добавить_пользователя(id=3, first_name="Единственный", role="admin")
    r = await client.delete("/api/users/3")
    assert r.status_code == 409, r.text
    текст = r.json()["detail"]
    assert "последний администратор" in текст, текст
    assert "назначьте другого" in текст


@pytest.mark.asyncio
async def test_обычного_сотрудника_отключить_МОЖНО(client, db, seeded):
    """⚠️ ТРЕТЬЯ ПРОВЕРКА ОБЯЗАТЕЛЬНА, требование владельца: защита, которая
    запрещает ВСЁ ПОДРЯД, выглядит рабочей и ломает обычную работу.
    Отключение рядового сотрудника обязано проходить как раньше.
    """
    r = await client.delete("/api/users/2")
    assert r.status_code == 200, r.text
    assert (await db.пользователь(2))["is_active"] is False, (
        "сотрудник обязан был погаснуть"
    )


@pytest.mark.asyncio
async def test_admin_still_manages_users(client, db, seeded):
    """Админ работает как раньше — иначе гейт «чинит» ценой поломки."""
    # ⚠️ Строка просящего (id=1) здесь НУЖНА: приглашение пишет created_by,
    # а это настоящий FK на users. Двойник внешних ключей не имел — живая
    # база требует, чтобы автор приглашения существовал, как и в проде.
    await db.добавить_пользователя(id=1, first_name="Админ", role="admin")
    # ⚠️ СОЗДАНИЕ ПЕРЕЕХАЛО НА ПРИГЛАШЕНИЕ (T104, этап ④): админ по-прежнему
    # заводит людей, но выписывая ссылку, а не строку без пароля.
    created = await client.post(
        "/api/invite/create",
        json={"role": "employee", "email": "p@example.com", "first_name": "Пётр"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["first_name"] == "Пётр"
    assert created.json()["invite_url"], "ссылка обязана вернуться"

    patched = await client.patch("/api/users/2", json={"inn": "123456789012"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["inn"] == "123456789012"

    gone = await client.delete("/api/users/2")
    assert gone.status_code == 200, gone.text
    assert (await db.пользователь(2))["is_active"] is False


# ────────────────── cards: admin или accountant (как категории) ─────────────


@pytest.mark.asyncio
async def test_employee_cannot_touch_cards(client_employee, db, seeded):
    было = len(await db.карты())
    assert (
        await client_employee.post("/api/cards/", json={"name": "Моя"})
    ).status_code == 403
    assert (
        await client_employee.patch("/api/cards/1", json={"name": "Переименована"})
    ).status_code == 403
    assert (await client_employee.patch("/api/cards/1/default")).status_code == 403
    assert (await client_employee.delete("/api/cards/1")).status_code == 403
    assert len(await db.карты()) == было
    assert (await db.карта(1))["name"] == "Корп.карта", (
        "карта не должна была измениться"
    )


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
    assert any(c["name"] == "Бухгалтерская" for c in await db.карты())


@pytest.mark.asyncio
async def test_admin_manages_cards(client, db, seeded):
    assert (
        await client.patch("/api/cards/1", json={"name": "Новая"})
    ).status_code == 200
    assert (await client.patch("/api/cards/1/default")).status_code == 200
    assert (await db.карта(1))["is_default"] is True
    assert (await client.delete("/api/cards/1")).status_code == 200
    assert await db.карта(1) is None


# ─────── слепые записи: ответ одинаков, проверять можно только состояние ──────


@pytest.mark.asyncio
async def test_admin_cannot_delete_card_of_other_org(client, db, seeded):
    """DELETE карты отвечает {"ok": true} ВСЕГДА — даже когда ничего не удалил.

    Найдено замером слепых записей (`tests/tools/blind_writes.py`): ручка
    не различает «удалил» и «нечего было удалять», поэтому её org-scope
    ПРИНЦИПИАЛЬНО не проверяется кодом ответа — только состоянием.
    """
    await db.добавить_карту(id=42, name="Чужая", org_id=2)
    r = await client.delete("/api/cards/42")
    assert r.status_code == 200, r.text
    assert await db.карта(42) is not None, (
        "админ одной орг удалил карту другой — org-scope в WHERE не работает"
    )
    assert await db.карта(1) is not None, "своя карта тоже должна быть цела"


@pytest.mark.asyncio
async def test_invite_with_unknown_role_is_rejected(client, db, seeded):
    """S-24: белый список ролей на единственной двери к `users.role`."""
    # ⚠️ S-24 ЖИВ, ТОЛЬКО ДВЕРЬ ОДНА (T104, этап ④): белый список `Role` стоит
    # на приглашении, и вторая дверь к `users.role` больше не существует.
    r = await client.post(
        "/api/invite/create", json={"role": "суперадмин", "email": "p@example.com"}
    )
    assert r.status_code == 422, r.text
    assert await db.число_пользователей() == 1, (
        "пользователь с чужой ролью не должен создаться"
    )
    assert await db.число_приглашений() == 0, (
        "и приглашение с чужой ролью не должно выписаться"
    )
