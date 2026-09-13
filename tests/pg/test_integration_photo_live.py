# -*- coding: utf-8 -*-
"""Снимок чека по ключу — на живой базе (шаг 3, заход ⑥).

⚠️ ЖИВАЯ БАЗА, А НЕ ДВОЙНИК, И ПО СУЩЕСТВУ. Предмет проверки — соединение
через `report_items` и статус отчёта: ключу доступен снимок ТОЛЬКО у чека,
лежащего в одобренном отчёте своей организации. Это условие исполняет SQL;
на двойнике его пришлось бы изображать, то есть проверять зеркало.

⚠️ И ВТОРОЕ, ЧТО ВИДНО ТОЛЬКО ЗДЕСЬ: все четыре отказа обязаны быть
НЕОТЛИЧИМЫ. Чужая организация, чек вне отчётов, чек в неодобренном отчёте,
чека нет вовсе — разные ответы позволили бы перебирать номера.
"""

import base64
from datetime import date

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import integration_keys as ик
from app.main import app
from app.storage import s3

ПРЕФИКС = "aocg-proba-1"
СЕКРЕТ = "proba-ne-sekret"
ЗАГОЛОВОК = {"Authorization": "Bearer " + ПРЕФИКС + "." + СЕКРЕТ}

БАЙТЫ_ХРАНИЛИЩА = b"\x89PNG-from-storage"
БАЙТЫ_БАЗЫ = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
B64_БАЗЫ = base64.b64encode(БАЙТЫ_БАЗЫ).decode("ascii")


@pytest.fixture
def хранилище_настроено(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT", "https://s3.example.ru")
    monkeypatch.setenv("S3_BUCKET", "aocg-receipts")
    monkeypatch.setenv("S3_REGION", "ru-1")
    monkeypatch.setenv("S3_ACCESS_KEY", "AKIDEXAMPLE")
    monkeypatch.setenv("S3_SECRET_KEY", "not-a-secret-for-tests")


@pytest.fixture
def хранилище_отдаёт(monkeypatch):
    async def подмена(cfg, key, timeout=15.0):
        return БАЙТЫ_ХРАНИЛИЩА, "image/png"

    monkeypatch.setattr(s3, "get_object", подмена)


@pytest_asyncio.fixture
async def клиент(db):
    """Клиент без подменённого человека: ручку открывает только ключ."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def клиент_человека(db):
    """Тот же стенд, но с человеком, — для заведомо разной пары по 302."""
    from app.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1,
        "org_id": 1,
        "email": "test@aocg.ru",
        "first_name": "Т",
        "last_name": "Т",
        "role": "admin",
        "is_email_verified": True,
        "password_hash": None,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


async def _основа(db):
    await db.добавить_организацию(id=1, name="АОЦГ")
    await db.добавить_пользователя(id=1, first_name="Алексей", role="admin", org_id=1)
    await db.pool.execute(
        "INSERT INTO integration_keys (org_id, prefix, secret_hash, name, created_by) "
        "VALUES (1, $1, $2, '1С бюро', 1)",
        ПРЕФИКС,
        ик.хеш_секрета(СЕКРЕТ),
    )


async def _чек(db, *, номер, org_id=1, ключ=None, адрес=None, база=None):
    await db.добавить_чек(
        id=номер,
        org="МЕРКА",
        amount=100.50,
        date=date(2026, 6, 11),
        org_id=org_id,
        user_id=1 if org_id == 1 else 9,
    )
    await db.pool.execute(
        "UPDATE receipts SET photo_key=$2, photo_url=$3, raw_data=$4 WHERE id=$1",
        номер,
        ключ,
        адрес,
        # ⚠️ СЛОВАРЁМ, А НЕ СТРОКОЙ. У пула стоит кодек `jsonb`, и строка уехала
        # бы в базу как JSON-СТРОКА, а не как объект: ручка потом не нашла бы
        # в ней снимка и ответила 404, то есть тест проверял бы свой же промах.
        {"photo_base64": база} if база else None,
    )


async def _отчёт(db, *, номер, статус="Одобрен", org_id=1, чеки=()):
    await db.добавить_отчёт(
        id=номер,
        title="Отчёт %d" % номер,
        user_id=1 if org_id == 1 else 9,
        total=100.50,
        status=статус,
        org_id=org_id,
        created=date(2026, 7, 1),
    )
    for чек in чеки:
        await db.положить_в_отчёт(report_id=номер, receipt_id=чек)


def _путь(номер):
    return "/api/integration/receipts/%d/photo" % номер


# ── ЧТО ОТДАЁТСЯ ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_снимок_из_хранилища_приходит_байтами(
    клиент, db, хранилище_настроено, хранилище_отдаёт
):
    await _основа(db)
    await _чек(db, номер=901, ключ="receipts/1/a.jpg")
    await _отчёт(db, номер=1, чеки=(901,))
    ответ = await клиент.get(_путь(901), headers=ЗАГОЛОВОК)
    assert ответ.status_code == 200, ответ.text
    assert ответ.content == БАЙТЫ_ХРАНИЛИЩА
    assert ответ.headers.get("cache-control") == "no-store"
    assert "receipts/1/a.jpg" not in ответ.text, "ключ объекта уехал наружу"


@pytest.mark.asyncio
async def test_снимок_из_базы_приходит_байтами(клиент, db):
    """Старые чеки со снимком внутри `raw_data` обязаны отдаваться так же."""
    await _основа(db)
    await _чек(db, номер=901, база=B64_БАЗЫ)
    await _отчёт(db, номер=1, чеки=(901,))
    ответ = await клиент.get(_путь(901), headers=ЗАГОЛОВОК)
    assert ответ.status_code == 200, ответ.text
    assert ответ.content == БАЙТЫ_БАЗЫ
    assert ответ.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_ключу_не_отдаётся_302_ни_в_каком_виде(клиент, db):
    """⚠️ РЕШЕНИЕ ВЛАДЕЛЬЦА 13.09.2026. Адрес уехал бы на чужую машину и зажил
    своей жизнью: отзыв ключа его не гасит. Хост при этом не из разрешённых,
    поэтому качать его нам тоже нельзя — отказ тот же 404."""
    await _основа(db)
    await _чек(db, номер=901, адрес="https://r2.example/a.jpg")
    await _отчёт(db, номер=1, чеки=(901,))
    ответ = await клиент.get(_путь(901), headers=ЗАГОЛОВОК, follow_redirects=False)
    assert ответ.status_code == 404, ответ.text
    assert "location" not in ответ.headers
    assert "r2.example" not in ответ.text, "адрес уехал наружу в теле отказа"


@pytest.mark.asyncio
async def test_человеку_на_том_же_чеке_302_остаётся(клиент_человека, db):
    """Заведомо разная пара: без неё «ключу не отдаётся» неотличимо от
    «не отдаётся никому», то есть от сломанной ветки."""
    await _основа(db)
    await _чек(db, номер=901, адрес="https://r2.example/a.jpg")
    await _отчёт(db, номер=1, чеки=(901,))
    ответ = await клиент_человека.get("/api/receipts/901/photo", follow_redirects=False)
    assert ответ.status_code == 302
    assert ответ.headers["location"] == "https://r2.example/a.jpg"
    assert ответ.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_порядок_источников_тот_же_что_у_человека(
    клиент, db, хранилище_настроено, хранилище_отдаёт
):
    """Ключ побеждает адрес и base64 — байты у трёх источников разные."""
    await _основа(db)
    await _чек(
        db,
        номер=901,
        ключ="receipts/1/new.jpg",
        адрес="https://r2.example/old.jpg",
        база=B64_БАЗЫ,
    )
    await _отчёт(db, номер=1, чеки=(901,))
    ответ = await клиент.get(_путь(901), headers=ЗАГОЛОВОК, follow_redirects=False)
    assert ответ.content == БАЙТЫ_ХРАНИЛИЩА
    assert ответ.content != БАЙТЫ_БАЗЫ
    assert "location" not in ответ.headers


# ── КОМУ НЕ ОТДАЁТСЯ ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_без_ключа_401(клиент, db):
    await _основа(db)
    await _чек(db, номер=901, база=B64_БАЗЫ)
    await _отчёт(db, номер=1, чеки=(901,))
    assert (await клиент.get(_путь(901))).status_code == 401


@pytest.mark.asyncio
async def test_четыре_отказа_неотличимы(клиент, db):
    """⚠️ ГЛАВНЫЙ СТОРОЖ ДОСТУПА. Все четыре случая обязаны совпасть и кодом,
    и телом: иначе по ответу перебираются номера чужих чеков."""
    await _основа(db)
    # ⚠️ В БАЗЕ ОБЯЗАН БЫТЬ ХОТЯ БЫ ОДИН ОДОБРЕННЫЙ ОТЧЁТ, И ЭТО НЕ ФОН.
    # Без него мутация «соединение с составом отчёта заменено на LEFT JOIN ON
    # TRUE» осталась зелёной: соединять было не с чем, и чек вне отчётов
    # получал 404 по пустоте, а не по правилу. Проверка молчала о своём же
    # промахе.
    await _чек(db, номер=900, база=B64_БАЗЫ)
    await _отчёт(db, номер=3, статус="Одобрен", чеки=(900,))
    # ① чек в НЕодобренном отчёте
    await _чек(db, номер=901, база=B64_БАЗЫ)
    await _отчёт(db, номер=1, статус="Черновик", чеки=(901,))
    # ② чек своей организации ВНЕ отчётов вовсе
    await _чек(db, номер=902, база=B64_БАЗЫ)
    # ③ чек ЧУЖОЙ организации в её одобренном отчёте
    await db.добавить_организацию(id=777, name="Чужая")
    await db.добавить_пользователя(id=9, first_name="Чужой", role="admin", org_id=777)
    await _чек(db, номер=903, org_id=777, база=B64_БАЗЫ)
    await _отчёт(db, номер=2, org_id=777, чеки=(903,))
    # ④ чека нет вовсе
    ответы = [
        await клиент.get(_путь(н), headers=ЗАГОЛОВОК) for н in (901, 902, 903, 9999)
    ]
    коды = {о.status_code for о in ответы}
    тела = {о.text for о in ответы}
    assert коды == {404}, "отказы разошлись кодом: %s" % коды
    assert len(тела) == 1, "отказы разошлись телом: %s" % тела


@pytest.mark.asyncio
async def test_одобренный_отчёт_на_тех_же_данных_снимок_отдаёт(клиент, db):
    """Вторая половина к проверке выше: без неё «404 на всё» выглядело бы
    правильным ответом сломанной ручки."""
    await _основа(db)
    await _чек(db, номер=901, база=B64_БАЗЫ)
    await _отчёт(db, номер=1, статус="Черновик", чеки=(901,))
    assert (await клиент.get(_путь(901), headers=ЗАГОЛОВОК)).status_code == 404
    await db.pool.execute("UPDATE reports SET status='Одобрен' WHERE id=1")
    assert (await клиент.get(_путь(901), headers=ЗАГОЛОВОК)).status_code == 200


@pytest.mark.asyncio
async def test_чек_без_снимка_даёт_тот_же_404(клиент, db):
    await _основа(db)
    await _чек(db, номер=901)
    await _отчёт(db, номер=1, чеки=(901,))
    assert (await клиент.get(_путь(901), headers=ЗАГОЛОВОК)).status_code == 404


@pytest.mark.asyncio
async def test_отозванный_ключ_снимок_не_получает(клиент, db):
    await _основа(db)
    await _чек(db, номер=901, база=B64_БАЗЫ)
    await _отчёт(db, номер=1, чеки=(901,))
    assert (await клиент.get(_путь(901), headers=ЗАГОЛОВОК)).status_code == 200
    await db.pool.execute("UPDATE integration_keys SET revoked_at = NOW()")
    assert (await клиент.get(_путь(901), headers=ЗАГОЛОВОК)).status_code == 401


@pytest.mark.asyncio
async def test_чек_лежит_не_более_чем_в_одном_отчёте(db):
    """⚠️ ЭТО ПРОВЕРКА ОСНОВАНИЯ, А НЕ РУЧКИ, И ОНА ЗАВЕДЕНА ПО ПРОМАХУ.

    Первая редакция запроса несла `LIMIT 1` с объяснением «чек бывает
    в нескольких отчётах». Живой прогон это опроверг: на связи стоит
    уникальность `uq_report_items_receipt_id`, один чек живёт не более чем
    в одном отчёте. `LIMIT 1` снят, а само основание закреплено здесь —
    снимут ограничение, и соединение начнёт множить строки молча.
    """
    await _основа(db)
    await _чек(db, номер=901, база=B64_БАЗЫ)
    await _отчёт(db, номер=1, статус="Черновик", чеки=(901,))
    await db.добавить_отчёт(
        id=2,
        title="Второй",
        user_id=1,
        total=1,
        status="Одобрен",
        created=date(2026, 7, 1),
    )
    with pytest.raises(Exception) as отказ:
        await db.положить_в_отчёт(report_id=2, receipt_id=901)
    assert "uq_report_items_receipt_id" in str(отказ.value)


@pytest.mark.asyncio
async def test_чек_свой_но_отчёт_чужой_организации_не_отдаётся(клиент, db):
    """⚠️ ЗАВЕДЕНО ПО ВЫЖИВШЕЙ МУТАЦИИ. Снятие `rep.org_id` из условия не
    краснило ничего: состояние «наш чек лежит в отчёте чужой организации»
    через приложение не возникает, и проверки его не описывали.

    Правило звучит «чек в одобренном отчёте СВОЕЙ организации», значит
    проверяться обязаны обе половины. Состояние собирается прямо в базе:
    связь `report_items` организацию не сверяет, и такая пара возможна.
    """
    await _основа(db)
    await db.добавить_организацию(id=777, name="Чужая")
    await db.добавить_пользователя(id=9, first_name="Чужой", role="admin", org_id=777)
    await _чек(db, номер=901, база=B64_БАЗЫ)
    await _отчёт(db, номер=2, org_id=777)
    await db.положить_в_отчёт(report_id=2, receipt_id=901)
    assert (await клиент.get(_путь(901), headers=ЗАГОЛОВОК)).status_code == 404
