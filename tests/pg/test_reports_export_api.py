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


def адреса_ссылок(байты: bytes) -> str:
    """Адреса внешних ссылок книги.

    ⚠️ ИХ НЕТ В ЛИСТЕ, И НА ЭТОМ ЛЕГКО ОБМАНУТЬСЯ: xlsx кладёт в ячейку только
    ПОДПИСЬ ссылки, а сам адрес уносит в отдельный файл связей. Проверка,
    ищущая адрес в тексте листа, была бы зелёной ВСЕГДА — и при рабочей
    ссылке, и при пустой ячейке.
    """
    with zipfile.ZipFile(BytesIO(байты)) as архив:
        имена = архив.namelist()
        связи = "xl/worksheets/_rels/sheet1.xml.rels"
        return архив.read(связи).decode("utf-8") if связи in имена else ""


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


# ─── СНИМОК ЧЕКА: ЕДИНСТВЕННОЕ, ЧТО ОСТАЛОСЬ В РОУТЕРЕ ПОСЛЕ ВЫНОСА ────
#
# ⚠️ ЗАЧЕМ ЭТИ ТРИ ПРОВЕРКИ И ПОЧЕМУ ИМЕННО ЗДЕСЬ. Заход ① вынес сбор данных
# в общий модуль, и сборщик ссылку на снимок больше НЕ СТАВИТ намеренно:
# машинному ответу подписанная ссылка противопоказана. Значит её ставит
# роутер — и это ЕДИНСТВЕННОЕ место, где вынос мог сломать файл незаметно:
# все четыре проверки выше пользуются чеком без снимка, то есть эту ветку
# не трогают вовсе. Пробел найден владельцем 12.09.2026 до перехода к ②.


async def _прицепить_снимок(db, receipt_id, *, ключ=None, адрес=None):
    """Фикстура чека снимка не ставит вовсе — дописываем прямо в базу."""
    await db.pool.execute(
        "UPDATE receipts SET photo_key=$1, photo_url=$2 WHERE id=$3",
        ключ,
        адрес,
        receipt_id,
    )


@pytest.mark.asyncio
async def test_подписанная_ссылка_на_снимок_доезжает_до_файла(
    client_accountant, db, monkeypatch
):
    """Хранилище настроено — в книге лежит ПОДПИСАННАЯ ссылка на объект."""
    id_отчёта = await _отчёт_с_чеком(db)
    await _прицепить_снимок(db, 900, ключ="receipts/900.jpg")
    monkeypatch.setenv("S3_ENDPOINT", "https://s3.example.ru/")
    monkeypatch.setenv("S3_BUCKET", "aocg-receipts")
    monkeypatch.setenv("S3_ACCESS_KEY", "id")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")

    ответ = await client_accountant.get("/api/reports/%s/export.xlsx" % id_отчёта)
    assert ответ.status_code == 200
    связи = адреса_ссылок(ответ.content)
    assert "receipts/900.jpg" in связи, "ссылка на снимок в файл не попала"
    # Подпись, а не голый адрес: без неё приватный бакет отдаст отказ.
    assert "X-Amz-Signature" in связи
    # И срок жизни ровно тот, что печатает колонка «Снимки действуют до»:
    # разойдись они — бухгалтер получит дату, которой ссылка не отвечает.
    assert "X-Amz-Expires=604800" in связи


@pytest.mark.asyncio
async def test_внешний_адрес_снимка_идёт_в_файл_как_есть(client_accountant, db):
    """У старых чеков снимок лежит внешним адресом — подписывать нечего."""
    id_отчёта = await _отчёт_с_чеком(db)
    await _прицепить_снимок(db, 900, адрес="https://example.org/старый.jpg")

    ответ = await client_accountant.get("/api/reports/%s/export.xlsx" % id_отчёта)
    assert ответ.status_code == 200
    assert "example.org" in адреса_ссылок(ответ.content)


@pytest.mark.asyncio
async def test_ненастроенное_хранилище_не_роняет_выгрузку(
    client_accountant, db, monkeypatch
):
    """Ключ есть, хранилища нет — ячейка пуста, а файл отдаётся.

    Это поведение записано в докстроке роутера: чек без ссылки на снимок
    остаётся полноценной строкой файла. Обратная половина пары к проверке
    выше: без неё «ссылка есть» неотличимо от «ссылка есть всегда».
    """
    id_отчёта = await _отчёт_с_чеком(db)
    await _прицепить_снимок(db, 900, ключ="receipts/900.jpg")
    for имя in ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY"):
        monkeypatch.delenv(имя, raising=False)

    ответ = await client_accountant.get("/api/reports/%s/export.xlsx" % id_отчёта)
    assert ответ.status_code == 200, "выгрузка обязана пройти и без хранилища"
    assert "receipts/900.jpg" not in адреса_ссылок(ответ.content)
    # И данные при этом на месте — пустой оказалась ровно одна ячейка.
    assert "МЕРКА" in текст_листа(ответ.content)
