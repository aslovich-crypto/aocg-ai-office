# -*- coding: utf-8 -*-
"""Ручка выгрузки отчёта в xlsx — проверки уровня HTTP, на живой базе.

⚠️ ЗАЧЕМ ЭТОТ ФАЙЛ ПОЯВИЛСЯ ТОЛЬКО СЕЙЧАС, И ЭТО НЕ УПРЁК ЗАДНИМ ЧИСЛОМ.
Шаг 2 закрыт приёмкой владельца 11.09.2026, и вся его проверка — юнит-тесты
СБОРЩИКА (`tests/test_report_xlsx.py`). Аудит шага 3 замерил: ручку
`GET /api/reports/{id}/export.xlsx` не покрывает ни один тест уровня HTTP,
поиск по строке «export.xlsx» во всём каталоге тестов давал НОЛЬ.

Пока сборщик и роутер жили одним куском, это было полбеды. Заход ① шага 3
выносит сбор из базы в общий модуль `app/reports_data` — и вот тут отсутствие
такого теста становится опасным: рефакторинг можно сделать «зелёным», не
проверив ни одной живой дороги, потому что проверять её нечем.

⚠️ ЖИВАЯ БАЗА, А НЕ ДВОЙНИК, И ПО СУЩЕСТВУ: предмет проверки — ЧТО ДОЕХАЛО
до файла, пройдя соединение со справочником статей, фильтр организации
и порядок сортировки. Ручка отвечает 200 и когда всё собралось, и когда
собралось пустое.
"""

import zipfile
from datetime import date
from io import BytesIO

import pytest

ТИП_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def текст_листа(байты: bytes) -> str:
    """Всё текстовое содержимое книги одной строкой.

    xlsx — это zip с XML; строки лежат в общей таблице `sharedStrings`.
    Разбирать по ячейкам здесь не нужно: состав колонок стережёт юнит-тест
    сборщика, а этому файлу важно другое — доехали ли ДАННЫЕ до файла вообще.
    """
    with zipfile.ZipFile(BytesIO(байты)) as архив:
        имена = архив.namelist()
        куски = []
        for имя in ("xl/sharedStrings.xml", "xl/worksheets/sheet1.xml"):
            if имя in имена:
                куски.append(архив.read(имя).decode("utf-8"))
    return "\n".join(куски)


async def _отчёт_с_чеком(db, org_id=1, user_id=1):
    """Отчёт из одного чека: организация, человек, категория, чек, состав."""
    await db.добавить_организацию(id=org_id)
    await db.добавить_пользователя(
        id=user_id,
        first_name="Алексей",
        last_name="Шукалович",
        role="admin",
        org_id=org_id,
    )
    await db.добавить_чек(
        id=900,
        org="МЕРКА",
        amount=100.50,
        date=date(2026, 6, 11),
        org_id=org_id,
        user_id=user_id,
    )
    await db.pool.execute(
        "INSERT INTO reports (id, title, status, total, created, org_id, user_id) "
        "VALUES (700, 'Июль', 'Одобрен', 100.50, $1, $2, $3)",
        date(2026, 7, 1),
        org_id,
        user_id,
    )
    await db.pool.execute(
        "INSERT INTO report_items (report_id, receipt_id) VALUES (700, 900)"
    )
    return 700


@pytest.mark.asyncio
async def test_бухгалтер_получает_файл_с_данными_отчёта(client_accountant, db):
    """Дорога целиком: запрос → сбор из базы → сборка книги → байты."""
    id_отчёта = await _отчёт_с_чеком(db)
    ответ = await client_accountant.get("/api/reports/%s/export.xlsx" % id_отчёта)
    assert ответ.status_code == 200
    assert ответ.headers["content-type"] == ТИП_XLSX
    # Имя файла отдаёт СЕРВЕР — фронт читает его отсюда, а не собирает рядом.
    assert "otchet-700-2026-07-01.xlsx" in ответ.headers["content-disposition"]
    # Выгрузка — персональные данные, в кэше промежуточных узлов им не место.
    assert ответ.headers["cache-control"] == "no-store"

    текст = текст_листа(ответ.content)
    assert "МЕРКА" in текст, "чек не доехал до файла"
    assert "Июль" in текст, "шапка отчёта не доехала до файла"
    assert "Шукалович А." in текст, "подотчётник не доехал до файла"


@pytest.mark.asyncio
async def test_сотруднику_выгрузка_закрыта(client_employee, db):
    """Право — бухгалтер и администратор: в карте ролей 1С-выгрузка за
    бухгалтером, и под ролью сотрудника реквизиты подотчётного лица
    в файл всё равно не попали бы (список людей ему режется)."""
    id_отчёта = await _отчёт_с_чеком(db)
    ответ = await client_employee.get("/api/reports/%s/export.xlsx" % id_отчёта)
    assert ответ.status_code == 403


@pytest.mark.asyncio
async def test_чужой_отчёт_неотличим_от_несуществующего(client_accountant, db):
    """org-scope: 404, а не 403 — чтобы чужой отчёт нельзя было нащупать
    перебором идентификаторов."""
    await _отчёт_с_чеком(db)
    await db.добавить_организацию(id=777, name="Чужая")
    await db.добавить_пользователя(id=9, first_name="Чужой", role="admin", org_id=777)
    await db.pool.execute(
        "INSERT INTO reports (id, title, status, total, created, org_id, user_id) "
        "VALUES (701, 'Чужой отчёт', 'Одобрен', 10, $1, 777, 9)",
        date(2026, 7, 1),
    )
    ответ = await client_accountant.get("/api/reports/701/export.xlsx")
    assert ответ.status_code == 404


@pytest.mark.asyncio
async def test_несуществующий_отчёт_даёт_404(client_accountant, db):
    await db.добавить_организацию(id=1)
    await db.добавить_пользователя(id=1, first_name="А", role="admin", org_id=1)
    ответ = await client_accountant.get("/api/reports/99999/export.xlsx")
    assert ответ.status_code == 404
