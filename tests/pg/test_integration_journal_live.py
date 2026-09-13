# -*- coding: utf-8 -*-
"""Журнал обращений по ключу — агрегат в трёх колонках (шаг 3, заход ⑦).

⚠️ ЖИВАЯ БАЗА, А НЕ ДВОЙНИК, И ПО СУЩЕСТВУ. Предмет проверки — ЧТО ЛЕГЛО
В СТРОКУ КЛЮЧА после запроса. Счётчик наращивает сама база (`use_count + 1`),
время ставит база (`NOW()`), а на двойнике оба пришлось бы изображать, то есть
проверять зеркало вместо приложения.

⚠️⚠️ СЕМАНТИКА ОТМЕТКИ, И ОНА ПРОВЕРЯЕТСЯ ОТДЕЛЬНЫМ ТЕСТОМ: «КЛЮЧ ПРИШЁЛ
И ПРОШЁЛ ПРОВЕРКУ», А НЕ «ДАННЫЕ ВЫДАНЫ». Обращение засчитывается и при 404 —
именно так виден перебор чужих номеров. Читать счётчик как «сколько документов
уехало» нельзя, и проверка ниже закрепляет это, а не документация.
"""

from datetime import date

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import integration_keys as ик
from app.main import app

ПРЕФИКС = "aocg-proba-1"
СЕКРЕТ = "proba-ne-sekret"
ЗАГОЛОВОК = {"Authorization": "Bearer " + ПРЕФИКС + "." + СЕКРЕТ}
ОКНО = "date_from=2026-01-01&date_to=2026-12-31"


@pytest_asyncio.fixture
async def клиент(db):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _основа(db):
    await db.добавить_организацию(id=1, name="АОЦГ")
    await db.добавить_пользователя(id=1, first_name="Алексей", role="admin", org_id=1)
    await db.pool.execute(
        "INSERT INTO integration_keys "
        "(org_id, prefix, secret_hash, name, created_by) "
        "VALUES (1, $1, $2, '1С бюро', 1)",
        ПРЕФИКС,
        ик.хеш_секрета(СЕКРЕТ),
    )


async def _отчёт(db, номер=1, статус="Одобрен"):
    await db.добавить_отчёт(
        id=номер,
        title="Отчёт",
        user_id=1,
        total=100,
        status=статус,
        created=date(2026, 7, 1),
    )


async def _строка_ключа(db):
    return dict(
        await db.pool.fetchrow(
            "SELECT * FROM integration_keys WHERE prefix=$1", ПРЕФИКС
        )
    )


# ── ЧТО ЗАПИСЫВАЕТСЯ ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_успешное_обращение_пишет_время_адрес_и_счёт(клиент, db):
    await _основа(db)
    await _отчёт(db)
    до = await _строка_ключа(db)
    assert до["use_count"] == 0 and до["last_used_at"] is None

    ответ = await клиент.get("/api/integration/reports?" + ОКНО, headers=ЗАГОЛОВОК)
    assert ответ.status_code == 200, ответ.text

    после = await _строка_ключа(db)
    assert после["use_count"] == 1
    assert после["last_used_at"] is not None
    assert после["last_used_ip"], "адрес не записан вовсе"


@pytest.mark.asyncio
async def test_счёт_растёт_на_каждое_обращение(клиент, db):
    await _основа(db)
    await _отчёт(db)
    for _ in range(3):
        await клиент.get("/api/integration/reports?" + ОКНО, headers=ЗАГОЛОВОК)
    assert (await _строка_ключа(db))["use_count"] == 3


@pytest.mark.asyncio
async def test_снимок_и_отчёт_считаются_одинаково(клиент, db):
    """Все три ручки идут через одну зависимость — считаются все три."""
    await _основа(db)
    await _отчёт(db)
    await клиент.get("/api/integration/reports?" + ОКНО, headers=ЗАГОЛОВОК)
    await клиент.get("/api/integration/reports/1", headers=ЗАГОЛОВОК)
    await клиент.get("/api/integration/receipts/1/photo", headers=ЗАГОЛОВОК)
    assert (await _строка_ключа(db))["use_count"] == 3


@pytest.mark.asyncio
async def test_адрес_берётся_из_x_forwarded_for(клиент, db):
    """Тем же извлекателем, что и у ограничителя частоты: починка S-35 обязана
    быть одна на оба места, а не две расходящиеся копии."""
    await _основа(db)
    await _отчёт(db)
    заголовки = dict(ЗАГОЛОВОК, **{"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
    await клиент.get("/api/integration/reports?" + ОКНО, headers=заголовки)
    assert (await _строка_ключа(db))["last_used_ip"] == "203.0.113.7"


# ── СЕМАНТИКА ОТМЕТКИ ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_обращение_засчитывается_и_при_404(клиент, db):
    """⚠️ ГЛАВНЫЙ СТОРОЖ СМЫСЛА. Отметка значит «ключ пришёл и прошёл проверку»,
    а не «данные выданы». Перебор чужих номеров обязан быть ВИДЕН в счётчике,
    иначе журнал не отвечает на вопрос, ради которого заведён."""
    await _основа(db)
    ответ = await клиент.get("/api/integration/reports/9999", headers=ЗАГОЛОВОК)
    assert ответ.status_code == 404
    assert (await _строка_ключа(db))["use_count"] == 1


@pytest.mark.asyncio
async def test_неверный_секрет_в_счёт_не_идёт(клиент, db):
    """Заведомо разная пара к проверке выше: неудачная попытка НЕ обращение.

    Иначе чужой перебор накручивал бы «использование» ключа, которым никто
    не воспользовался, и отличить одно от другого стало бы нечем.
    """
    await _основа(db)
    чужой = {"Authorization": "Bearer " + ПРЕФИКС + ".ne-tot-sekret"}
    ответ = await клиент.get("/api/integration/reports?" + ОКНО, headers=чужой)
    assert ответ.status_code == 401
    строка = await _строка_ключа(db)
    assert строка["use_count"] == 0 and строка["last_used_at"] is None


@pytest.mark.asyncio
async def test_отозванный_ключ_в_счёт_не_идёт(клиент, db):
    await _основа(db)
    await db.pool.execute("UPDATE integration_keys SET revoked_at = NOW()")
    ответ = await клиент.get("/api/integration/reports?" + ОКНО, headers=ЗАГОЛОВОК)
    assert ответ.status_code == 401
    assert (await _строка_ключа(db))["use_count"] == 0


# ── ЧЕГО В ЖУРНАЛЕ БЫТЬ НЕ ДОЛЖНО ────────────────────────────────────────


@pytest.mark.asyncio
async def test_секрета_в_строке_ключа_нет_ни_в_каком_виде(клиент, db):
    """⚠️ ПОИМЁННО ПО ВСЕЙ СТРОКЕ, А НЕ ПО НАЗВАННЫМ КОЛОНКАМ. Проверка
    «в last_used_ip нет секрета» промолчала бы, появись завтра колонка
    с ним рядом."""
    await _основа(db)
    await _отчёт(db)
    await клиент.get("/api/integration/reports?" + ОКНО, headers=ЗАГОЛОВОК)
    строка = await _строка_ключа(db)
    целиком = " ".join(str(з) for з in строка.values())
    assert СЕКРЕТ not in целиком, "секрет осел в строке ключа"
    assert "Bearer" not in целиком, "заголовок Authorization осел в строке ключа"
    # Префикс — открытая часть по устройству, он там быть ОБЯЗАН.
    assert строка["prefix"] == ПРЕФИКС


@pytest.mark.asyncio
async def test_данных_документа_в_журнале_нет(клиент, db):
    """Журнал отвечает «кто ходил», а не «что в чеке»: попав туда, данные
    пережили бы и удаление чека, и отзыв ключа."""
    await _основа(db)
    await _отчёт(db)
    await db.добавить_чек(
        id=901,
        org="МЕРКА-ЗАМЕТНАЯ-СТРОКА",
        amount=100.50,
        date=date(2026, 6, 11),
        org_id=1,
        user_id=1,
    )
    await db.положить_в_отчёт(report_id=1, receipt_id=901)
    await клиент.get("/api/integration/reports/1", headers=ЗАГОЛОВОК)
    строка = await _строка_ключа(db)
    целиком = " ".join(str(з) for з in строка.values())
    assert "МЕРКА-ЗАМЕТНАЯ-СТРОКА" not in целиком


# ── ОТКАЗ ЖУРНАЛА НЕ ОТКАЗ ВЫДАЧИ ────────────────────────────────────────


@pytest.mark.asyncio
async def test_упавший_журнал_не_роняет_выдачу(клиент, db, monkeypatch):
    """⚠️ ТРЕБОВАНИЕ ВЛАДЕЛЬЦА 13.09.2026. Журнал вспомогательный: упавшая
    запись — не повод не отдать бухгалтеру его же документы."""
    await _основа(db)
    await _отчёт(db)

    # ⚠️ ЛОМАЕМ САМ ЗАПРОС, А НЕ ОБЁРТКУ. Подменив функцию целиком, мы
    # проверили бы свою же заглушку; сломанный SQL проходит настоящий путь
    # до базы и обратно — ровно так журнал и откажет в жизни.
    monkeypatch.setattr(ик, "ОТМЕТКА", "UPDATE integration_keys SET нет_такой = 1")
    ответ = await клиент.get("/api/integration/reports?" + ОКНО, headers=ЗАГОЛОВОК)
    assert ответ.status_code == 200, ответ.text
    assert len(ответ.json()["reports"]) == 1
    # Счётчик при этом не вырос — отметка честно не записалась.
    assert (await _строка_ключа(db))["use_count"] == 0


@pytest.mark.asyncio
async def test_отмечается_ровно_тот_ключ_которым_пришли(клиент, db):
    """⚠️ ЗАБЫТОЕ УСЛОВИЕ ОТБОРА ОТМЕТИЛО БЫ ВСЕ КЛЮЧИ РАЗОМ, И СНАРУЖИ ЭТО
    выглядело бы как «ключом пользуются» у ключа, который лежит без дела.
    Второй ключ здесь не фон: без него проверка счётчика зелена и при
    отсутствии условия отбора вовсе."""
    await _основа(db)
    await _отчёт(db)
    await db.pool.execute(
        "INSERT INTO integration_keys "
        "(org_id, prefix, secret_hash, name, created_by) "
        "VALUES (1, $1, $2, 'второй', 1)",
        "aocg-proba-2",
        ик.хеш_секрета(СЕКРЕТ),
    )
    await клиент.get("/api/integration/reports?" + ОКНО, headers=ЗАГОЛОВОК)
    второй = dict(
        await db.pool.fetchrow(
            "SELECT * FROM integration_keys WHERE prefix='aocg-proba-2'"
        )
    )
    assert (await _строка_ключа(db))["use_count"] == 1
    assert второй["use_count"] == 0, "отметился чужой ключ"
    assert второй["last_used_at"] is None
