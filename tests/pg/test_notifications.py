# -*- coding: utf-8 -*-
"""События уведомлений при смене статуса отчёта (T159) — на живой базе.

⚠️ ПОЧЕМУ ЗДЕСЬ, А НЕ НА ДВОЙНИКЕ. Проверяется, что строка события легла
в базу С ПРАВИЛЬНЫМ АДРЕСАТОМ — то есть ровно то, что двойник изображал бы
по нашему же описанию. Адресат вычисляется запросом (кто автор отчёта, кто
управляющие организации), и подменять этот запрос толкованием — значит
проверять собственную выдумку (класс T136).
"""

from datetime import date

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def контора(db):
    """Админ (id=1, он же смотрящий), бухгалтер и сотрудник-автор."""
    await db.добавить_пользователя(
        id=1, first_name="Админ", role="admin", email="admin@example.com"
    )
    await db.добавить_пользователя(
        id=5, first_name="Бух", role="accountant", email="buh@example.com"
    )
    await db.добавить_пользователя(
        id=2,
        first_name="Иван",
        last_name="Петров",
        role="employee",
        email="ivan@example.com",
    )
    await db.добавить_отчёт(
        id=1, title="Отчёт за май", user_id=2, total=5000, created=date(2026, 5, 10)
    )
    return db


async def события(db, кому=None):
    условие = " WHERE user_id=$1" if кому else ""
    строки = await db.pool.fetch(
        f"SELECT * FROM notifications{условие} ORDER BY id", *([кому] if кому else [])
    )
    return [dict(с) for с in строки]


@pytest.mark.asyncio
async def test_отклонение_без_причины_отвергается(client, db, контора):
    """⚠️ ТРЕБОВАНИЕ ВЛАДЕЛЬЦА: без причины уведомление не экономит ничего.

    Человек всё равно пойдёт выяснять, что не так, — значит письмо и точка
    были потрачены зря. Поэтому отказ без причины не принимается вовсе.
    """
    r = await client.patch("/api/reports/1", json={"status": "Отклонён"})
    assert r.status_code == 400, r.text
    assert "причину" in r.json()["detail"].lower()
    assert await события(db) == [], "события быть не должно — отказ не состоялся"


@pytest.mark.asyncio
async def test_отклонение_с_причиной_доходит_до_автора(client, db, контора):
    """Главное событие: деньги не вернули, и человек узнаёт об этом сразу."""
    r = await client.patch(
        "/api/reports/1",
        json={"status": "Отклонён", "reason": "нет чека на 1200 ₽"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["reject_reason"] == "нет чека на 1200 ₽", "причина живёт в отчёте"

    строки = await события(db)
    assert len(строки) == 1, "ровно одно событие — автору"
    (событие,) = строки
    assert событие["user_id"] == 2, "адресат — автор отчёта, а не тот, кто отклонил"
    assert событие["kind"] == "report_rejected"
    assert событие["body"] == "нет чека на 1200 ₽", "причина уходит в уведомление"
    assert событие["report_id"] == 1
    assert событие["read_at"] is None, "новое событие непрочитано"


@pytest.mark.asyncio
async def test_одобрение_доходит_до_автора(client, db, контора):
    r = await client.patch("/api/reports/1", json={"status": "Одобрен"})
    assert r.status_code == 200, r.text
    (событие,) = await события(db)
    assert событие["user_id"] == 2 and событие["kind"] == "report_approved"


@pytest.mark.asyncio
async def test_отправка_на_проверку_будит_всех_управляющих(
    client_employee, db, контора
):
    """⚠️ КОМУ — ОТВЕТ ВЛАДЕЛЬЦА: всем, у кого есть право видеть отчёт.

    И каждому СВОЯ строка: иначе «бухгалтер прочёл» гасило бы точку
    у администратора.
    """
    r = await client_employee.patch("/api/reports/1", json={"status": "На проверке"})
    assert r.status_code == 200, r.text
    строки = await события(db)
    assert {с["user_id"] for с in строки} == {1, 5}, "админ и бухгалтер"
    assert all(с["kind"] == "report_submitted" for с in строки)
    assert all("Иван Петров" in (с["body"] or "") for с in строки), (
        "управляющему нужно знать, ЧЕЙ отчёт пришёл"
    )


@pytest.mark.asyncio
async def test_себе_событие_не_пишется(client, db, контора):
    """⚠️ ПРАВИЛО ВЛАДЕЛЬЦА: уведомление, которое человек может предсказать
    сам, обесценивает остальные.

    Здесь отчёт принадлежит САМОМУ смотрящему (админу): он же его и одобряет.
    Событие в этом случае — шум, и его быть не должно.
    """
    await db.добавить_отчёт(id=7, title="Свой отчёт", user_id=1, total=100)
    r = await client.patch("/api/reports/7", json={"status": "Одобрен"})
    assert r.status_code == 200, r.text
    assert await события(db) == []


@pytest.mark.asyncio
async def test_чужая_организация_событий_не_получает(client, db, контора):
    """org-scope: управляющий чужой организации не адресат наших событий."""
    await db.добавить_пользователя(
        id=90, first_name="Чужой", role="admin", org_id=777, email="x@example.com"
    )
    await client.patch("/api/reports/1", json={"status": "Отклонён", "reason": "мимо"})
    строки = await события(db)
    assert all(с["user_id"] != 90 for с in строки)
    assert all(с["org_id"] == 1 for с in строки)


@pytest.mark.asyncio
async def test_погашенный_управляющий_событий_не_получает(client_employee, db, контора):
    """⚠️ ДЫРУ НАШЛА МУТАЦИЯ, А НЕ ЧТЕНИЕ КОДА (04.09.2026).

    Снятое из запроса `AND is_active = true` не покраснело НИ НА ОДНОМ
    из шести тестов: уволенный бухгалтер продолжал бы получать отчёты
    своей бывшей организации, и заметить это было бы нечем.
    """
    await db.погасить(5)
    r = await client_employee.patch("/api/reports/1", json={"status": "На проверке"})
    assert r.status_code == 200, r.text
    строки = await события(db)
    assert {с["user_id"] for с in строки} == {1}, "только живой админ"


# ───────────────────── ручки колокольчика ────────────────────────────────────


@pytest.mark.asyncio
async def test_список_отдаёт_только_свои_события(client, db, контора):
    """⚠️ ЧУЖИХ УВЕДОМЛЕНИЙ НЕ ВИДИТ НИКТО, включая администратора.

    Событие — личная почта, а не общий журнал; смотрящий здесь админ,
    и он НЕ должен видеть событие сотрудника.
    """
    await client.patch(
        "/api/reports/1", json={"status": "Отклонён", "reason": "нет чека"}
    )
    r = await client.get("/api/notifications/")
    assert r.status_code == 200, r.text
    тело = r.json()
    assert тело["items"] == [] and тело["unread"] == 0, тело


@pytest.mark.asyncio
async def test_адресат_видит_событие_и_точку(client, as_role, db, контора):
    """⚠️ ОДИН КЛИЕНТ, РОЛЬ МЕНЯЕТСЯ ЯВНО. Две клиентские фикстуры в одном
    тесте перетирают общую подмену `get_current_user`, и оба запроса уходят
    от последнего смотрящего — первая редакция теста так и делала: админ
    «отклонял» отчёт, будучи сотрудником, получал 403, и событие не рождалось
    вовсе."""
    await client.patch(
        "/api/reports/1", json={"status": "Отклонён", "reason": "нет чека на 1200 ₽"}
    )
    as_role("employee", user_id=2)
    r = await client.get("/api/notifications/")
    тело = r.json()
    assert тело["unread"] == 1, "точка обязана загореться"
    (событие,) = тело["items"]
    assert событие["kind"] == "report_rejected"
    assert событие["body"] == "нет чека на 1200 ₽", "причина видна в списке"
    assert событие["read"] is False


@pytest.mark.asyncio
async def test_открытие_списка_гасит_точку(client, as_role, db, контора):
    """Решение владельца: прочитанность — открытием списка, а не поштучно."""
    await client.patch(
        "/api/reports/1", json={"status": "Отклонён", "reason": "нет чека"}
    )
    as_role("employee", user_id=2)
    прочитано = await client.post("/api/notifications/read")
    assert прочитано.json() == {"read": 1}
    тело = (await client.get("/api/notifications/")).json()
    assert тело["unread"] == 0, "точка погасла"
    assert тело["items"][0]["read"] is True, "но само событие осталось в списке"


@pytest.mark.asyncio
async def test_чужие_события_не_гасятся(client, as_role, db, контора):
    """Пометка прочитанным трогает только свои строки."""
    await db.pool.execute(
        "INSERT INTO notifications (user_id, org_id, kind, title) "
        "VALUES (1, 1, 'report_submitted', 'Чужое')"
    )
    await client.patch("/api/reports/1", json={"status": "Одобрен"})
    as_role("employee", user_id=2)
    await client.post("/api/notifications/read")
    чужое = await db.pool.fetchrow(
        "SELECT read_at FROM notifications WHERE user_id=1 AND title='Чужое'"
    )
    assert чужое["read_at"] is None, "сотрудник погасил событие администратора"
