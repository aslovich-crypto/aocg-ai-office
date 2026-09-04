# -*- coding: utf-8 -*-
"""Целостность отчёта: чек из отчёта не удаляется, одобрение снимает утверждающий.

⚠️ ЗАЧЕМ, УЛИКОЙ С ПРОДА (замер владельца 04.09.2026 через бастион). Отчёт
id 10 «Проверка», статус «На проверке»: total = 1.00, чеков в составе — НОЛЬ.
Расхождение на 1.00 ₽ появилось само, с первого раза, на живых данных.
Причина: одиночное `DELETE /api/receipts/{id}` чистило `report_items` и уходило,
не трогая `reports.total`, — а массовое удаление этот же случай блокировало
всегда. Одно правило жило в двух местах, и одно из них о нём не знало.

⚠️ ЧТО ИМЕННО ПРОВЕРЯЕТСЯ. Не «вернулся ли отказ» — отказ можно вернуть и всё
равно удалить, — а СОСТОЯНИЕ после попытки: чек на месте, состав на месте,
total равен сумме чеков состава. Отказ проверяется отдельной строкой, состояние
отдельной; тест, который смотрит только на код ответа, пропустил бы ровно тот
дефект, что дал отчёт id 10.

Фикстуры (tests/pg/conftest): client=admin(id=1), client_accountant(id=1),
client_employee(id=2).
"""

from datetime import date

import pytest
import pytest_asyncio

ADMIN_ID = 1  # client (admin) и client_accountant ходят под user_id=1
EMP_ID = 2  # client_employee — user_id=2
ORG = 1
СТАТУСЫ = ("Черновик", "На проверке", "Одобрен", "Отклонён")


@pytest_asyncio.fixture
async def люди(db):
    await db.добавить_пользователя(id=ADMIN_ID, first_name="Админ", role="admin")
    await db.добавить_пользователя(id=EMP_ID, first_name="Иван", role="employee")
    return db


async def _отчёт_с_чеком(db, *, статус, автор, rid=101, репид=501, сумма=100):
    """Отчёт из одного чека: total — снимок, равный составу (как в проде)."""
    await db.добавить_чек(
        id=rid,
        org="ООО Тест",
        amount=сумма,
        date=date(2026, 6, 1),
        payment="Карта",
        org_id=ORG,
        user_id=автор,
    )
    await db.добавить_отчёт(
        id=репид, title="Отчёт с чеком", user_id=автор, total=сумма, status=статус
    )
    await db.положить_в_отчёт(репид, rid)
    return rid, репид


async def _сошлось(db, репид):
    """ГЛАВНЫЙ ИНВАРИАНТ: снимок суммы равен тому, что реально в составе."""
    отчёт = await db.отчёт(репид)
    return отчёт["total"] == await db.сумма_состава(репид)


# ── 4+5: удаление чека из отчёта отказано, все статусы × все роли ────────────
@pytest.mark.parametrize("статус", СТАТУСЫ)
@pytest.mark.asyncio
async def test_admin_не_удаляет_чек_из_отчёта_в_любом_статусе(db, люди, client, статус):
    # ⚠️ ПРАВИЛО ОДНО ДЛЯ ВСЕХ, ВКЛЮЧАЯ admin (решение владельца): сумма отчёта
    # не может зависеть от того, кто нажал «Удалить».
    rid, репид = await _отчёт_с_чеком(db, статус=статус, автор=EMP_ID)
    r = await client.delete(f"/api/receipts/{rid}")
    assert r.status_code == 409
    assert "Отчёт с чеком" in r.json()["detail"]  # отказ НАЗЫВАЕТ отчёт
    assert await db.чек(rid) is not None
    assert await _сошлось(db, репид)


@pytest.mark.parametrize("статус", СТАТУСЫ)
@pytest.mark.asyncio
async def test_accountant_не_удаляет_свой_чек_из_отчёта(
    db, люди, client_accountant, статус
):
    # Чек СВОЙ: чужой бухгалтер не удаляет и без этой проверки, и тест тогда
    # проходил бы по старой причине, ничего не доказывая про новую.
    rid, репид = await _отчёт_с_чеком(db, статус=статус, автор=ADMIN_ID)
    r = await client_accountant.delete(f"/api/receipts/{rid}")
    assert r.status_code == 409
    assert await db.чек(rid) is not None
    assert await _сошлось(db, репид)


@pytest.mark.parametrize("статус", СТАТУСЫ)
@pytest.mark.asyncio
async def test_employee_не_удаляет_свой_чек_из_отчёта(
    db, люди, client_employee, статус
):
    rid, репид = await _отчёт_с_чеком(db, статус=статус, автор=EMP_ID)
    r = await client_employee.delete(f"/api/receipts/{rid}")
    assert r.status_code == 409
    assert await db.чек(rid) is not None
    assert await _сошлось(db, репид)


@pytest.mark.asyncio
async def test_чек_вне_отчёта_удаляется_как_прежде(db, люди, client):
    """Проверка не должна запрещать обычное удаление — иначе «починили» бы
    целостность ценой работы, ради которой кнопка и стоит."""
    await db.добавить_чек(
        id=102,
        org="ООО Тест",
        amount=100,
        date=date(2026, 6, 1),
        payment="Карта",
        org_id=ORG,
        user_id=EMP_ID,
    )
    r = await client.delete("/api/receipts/102")
    assert r.status_code == 200
    assert await db.чек(102) is None


@pytest.mark.asyncio
async def test_чужой_чек_в_отчёте_не_выдаёт_себя_названием(db, люди, client_employee):
    """⚠️ Anti-enumeration: чужой чек по-прежнему неотличим от несуществующего.
    Отказ 409 называет отчёт — значит его нельзя показывать тому, кто и удалять
    этот чек не вправе: иначе новая проверка стала бы разведкой по чужим чекам."""
    rid, _ = await _отчёт_с_чеком(db, статус="Черновик", автор=ADMIN_ID)
    r = await client_employee.delete(f"/api/receipts/{rid}")
    assert r.status_code == 200  # как и раньше: молча, без подробностей
    assert await db.чек(rid) is not None


@pytest.mark.asyncio
async def test_массовое_удаление_блокирует_тем_же_правилом(db, люди, client):
    """«Одно место на оба пути»: bulk обязан блокировать тот же чек, и после
    попытки состояние обязано сойтись."""
    rid, репид = await _отчёт_с_чеком(db, статус="Черновик", автор=EMP_ID)
    r = await client.post(
        "/api/receipts/bulk-delete", json={"ids": [rid], "force": True}
    )
    assert r.status_code == 200
    assert r.json()["blocked_in_report"] == [rid]
    assert r.json()["deleted"] == []
    assert await db.чек(rid) is not None
    assert await _сошлось(db, репид)


# ── 6: сторож целостности отдельно от кода ответа ───────────────────────────
@pytest.mark.parametrize("статус", СТАТУСЫ)
@pytest.mark.asyncio
async def test_состояние_сходится_после_любой_попытки_удаления(
    db, люди, client, client_employee, статус
):
    """⚠️ ГЛАВНЫЙ СТОРОЖ. Не «что ответили», а «что стало»: три попытки подряд
    разными ролями и путями, после каждой — состав на месте и total равен сумме.
    Ровно этого сторожа не было, и отчёт id 10 доехал до прода."""
    rid, репид = await _отчёт_с_чеком(db, статус=статус, автор=EMP_ID)
    было = await db.сумма_состава(репид)

    await client.delete(f"/api/receipts/{rid}")  # admin, одиночное
    await client_employee.delete(f"/api/receipts/{rid}")  # автор, одиночное
    await client.post("/api/receipts/bulk-delete", json={"ids": [rid], "force": True})

    assert await db.сумма_состава(репид) == было  # состав не поехал
    assert await _сошлось(db, репид)  # и снимок с ним сходится


# ── 7: снятие одобрения ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_admin_снимает_одобрение(db, люди, client):
    await db.добавить_отчёт(
        id=601, title="Одобренный", user_id=EMP_ID, total=0, status="Одобрен"
    )
    r = await client.patch("/api/reports/601", json={"status": "Черновик"})
    assert r.status_code == 200
    assert (await db.отчёт(601))["status"] == "Черновик"


@pytest.mark.asyncio
async def test_accountant_снимает_одобрение(db, люди, client_accountant):
    await db.добавить_отчёт(
        id=602, title="Одобренный", user_id=EMP_ID, total=0, status="Одобрен"
    )
    r = await client_accountant.patch("/api/reports/602", json={"status": "Черновик"})
    assert r.status_code == 200
    assert (await db.отчёт(602))["status"] == "Черновик"


@pytest.mark.asyncio
async def test_автор_сотрудник_одобрение_не_снимает(db, люди, client_employee):
    """Свой отчёт, свой автор — и всё равно нельзя: отчёт, принятый к учёту,
    перестаёт быть личным делом автора (решение владельца 04.09.2026)."""
    await db.добавить_отчёт(
        id=603, title="Одобренный", user_id=EMP_ID, total=0, status="Одобрен"
    )
    r = await client_employee.patch("/api/reports/603", json={"status": "Черновик"})
    assert r.status_code == 403
    assert (await db.отчёт(603))["status"] == "Одобрен"  # статус НЕ поехал


@pytest.mark.asyncio
async def test_автор_отправляет_и_отзывает_как_прежде(db, люди, client_employee):
    """Гейт стоит на выходе ИЗ «Одобрен», а не на любом переходе: обычная
    работа автора — отправить и отозвать — должна остаться."""
    await db.добавить_отчёт(
        id=604, title="Черновик", user_id=EMP_ID, total=0, status="Черновик"
    )
    вперёд = await client_employee.patch(
        "/api/reports/604", json={"status": "На проверке"}
    )
    назад = await client_employee.patch("/api/reports/604", json={"status": "Черновик"})
    assert вперёд.status_code == 200 and назад.status_code == 200
    assert (await db.отчёт(604))["status"] == "Черновик"
