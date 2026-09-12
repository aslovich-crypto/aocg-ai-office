# -*- coding: utf-8 -*-
"""Ручка отдачи одобренных отчётов по ключу — на живой базе (шаг 3, заход ⑤).

⚠️ ЖИВАЯ БАЗА, А НЕ ДВОЙНИК. Предмет проверки — ЧТО ОТОБРАЛ SQL и ЧТО УЕХАЛО
НАРУЖУ. Ручка отвечает 200 и когда отдала лишнее поле, и когда отдала ровно
договорённое; различить это можно только по содержимому ответа на настоящих
строках, с настоящим соединением справочника и настоящим фильтром организации.

⚠️⚠️ СОСТАВ ОТВЕТА СВЕРЯЕТСЯ С ПИСАНЫМ СПИСКОМ В ЭТОМ ФАЙЛЕ, А НЕ
С КОНСТАНТОЙ ИЗ КОДА. Сверка вида `set(ответ) == set(инт.ПОЛЯ_ЧЕКА)` —
тавтология: дописав поле в белый список, её автор оставит тест зелёным
и ничего не заметит. Именно этого требовал владелец 12.09.2026 — «поимённо,
как со словарём принципала». Значит имена перечислены ЗДЕСЬ, руками, и
расширение белого списка обязано краснить этот файл. Покраснел — значит
кто-то должен объяснить, зачем новому полю уезжать на чужую машину.
"""

from datetime import date, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import integration_keys as ик
from app.main import app
from app.routers import integration as инт

ПРЕФИКС = "aocg-proba-1"
# ⚠️ СЕКРЕТ ЗДЕСЬ ЛАТИНИЦЕЙ, И ЭТО НЕ ПРИДИРКА. Ключ едет в заголовке
# `Authorization`, а заголовки HTTP — ASCII: кириллица в секрете роняет
# запрос ещё в клиенте, до всякой проверки. Значит выпуск ключей (заход ⑧)
# обязан порождать секрет из ASCII, и этот тест — первое место, где это
# видно. Слова выбраны заведомо не похожими на настоящий секрет: сторож
# gitleaks режет коммит по энтропии строки, а не по её смыслу.
СЕКРЕТ = "proba-ne-sekret"
ПОЛНЫЙ = ПРЕФИКС + "." + СЕКРЕТ
ЗАГОЛОВОК = {"Authorization": "Bearer " + ПОЛНЫЙ}

# ⚠️ ПИСАНЫЙ ДОГОВОР. Эти три набора — копия белых списков, сделанная РУКАМИ
# и намеренно: см. докстроку модуля.
ОЖИДАЕМЫЕ_ПОЛЯ_ОТЧЁТА = {"id", "title", "status", "total", "created"}

ОЖИДАЕМЫЕ_ПОЛЯ_ЧЕКА = {
    "id",
    "date",
    "datetime",
    "org",
    "org_legal",
    "org_inn",
    "address",
    "amount",
    "currency",
    "operation_type",
    "payment",
    "payment_form",
    "card_last4",
    "tax_system",
    "kkt_fn",
    "fd_num",
    "fpd",
    "kkt_rn",
    "kkt_serial",
    "cashier",
    "vat_total",
    "sum_vat_0",
    "sum_no_vat",
    "vat_breakdown",
    "category_name",
}

# Поля, которые ручка добавляет сама, поверх белых списков.
ДОБАВОЧНЫЕ_ОТЧЁТА = {"employee_name", "receipts"}
ДОБАВОЧНОЕ_ЧЕКА = {"has_photo"}

# ⚠️ ЧЕМУ НАРУЖУ НЕЛЬЗЯ — ТОЖЕ ПОИМЁННО, А НЕ ПРОВЕРКОЙ «НЕТ ЛИШНЕГО».
# Общая проверка не различает «поля нет» и «поле переименовали», а именно
# так запретное и просачивается обратно.
ЗАПРЕТНЫЕ = (
    "raw_data",
    "photo_base64",
    "photo_key",
    "photo_url",
    "org_id",
    "user_id",
    "card_id",
    "category_id",
    "category",
    "category_manual",
    "created_at",
    "employee",
    "ocr_fd",
    "ocr_fpd",
    "vat_0",
    "org_brand",
    "payment_detail",
    "source",
    "secret_hash",
)

СНИМОК = "СНИМОК-НЕ-ДОЛЖЕН-УТЕЧЬ"

# ⚠️ РАСПОЗНАННЫЕ РЕКВИЗИТЫ — ЗАВЕДОМО НЕПОХОЖИМИ НА ЧИСЛА. Первая редакция
# положила сюда «12345», и проверка покраснела зря: ровно эта цепочка стоит
# внутри ИНН `7701234567`. Поиск подстроки в ответе не различает, ОТКУДА она
# там; значит метка обязана быть такой, какой больше нигде взяться неоткуда.
ОЦР_ФД = "OCR-FD-METKA"
ОЦР_ФПД = "OCR-FPD-METKA"


@pytest_asyncio.fixture
async def клиент(db):
    """Клиент БЕЗ подменённого человека: ручку открывает только ключ.

    ⚠️ Остальные живые клиенты подменяют `get_current_user`, и под ними
    проверка ключа была бы неотличима от «пустили, потому что вошёл человек».
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _основа(db):
    await db.добавить_организацию(id=1, name="АОЦГ")
    await db.добавить_пользователя(
        id=1, first_name="Алексей", last_name="Шукалович", role="admin", org_id=1
    )
    await db.pool.execute(
        "INSERT INTO integration_keys (org_id, prefix, secret_hash, name, created_by) "
        "VALUES (1, $1, $2, '1С бюро', 1)",
        ПРЕФИКС,
        ик.хеш_секрета(СЕКРЕТ),
    )


async def _отчёт(db, *, id_, статус="Одобрен", создан=date(2026, 7, 1), с_чеком=True):
    await db.добавить_отчёт(
        id=id_,
        title="Отчёт %d" % id_,
        user_id=1,
        total=100.50,
        status=статус,
        created=создан,
    )
    if not с_чеком:
        return
    номер = 900 + id_
    await db.добавить_чек(
        id=номер,
        org="МЕРКА",
        amount=100.50,
        date=date(2026, 6, 11),
        org_id=1,
        user_id=1,
        org_brand="Мерка у дома",
        org_legal='ООО "Мерка"',
        org_inn="7701234567",
    )
    # Поля, которых нет у помощника: снимок, распознанные реквизиты, время.
    await db.pool.execute(
        "UPDATE receipts SET raw_data=$2, photo_key=$3, ocr_fd=$4, ocr_fpd=$5, "
        "payment_detail=$6, datetime=$7 WHERE id=$1",
        номер,
        '{"photo_base64": "%s", "items": []}' % СНИМОК,
        "receipts/900.jpg",
        ОЦР_ФД,
        ОЦР_ФПД,
        "карта VISA",
        datetime(2026, 6, 11, 11, 45),
    )
    await db.положить_в_отчёт(report_id=id_, receipt_id=номер)


def _список(**правки):
    путь = "/api/integration/reports?date_from=2026-01-01&date_to=2026-12-31"
    for имя, значение in правки.items():
        путь += "&%s=%s" % (имя, значение)
    return путь


# ── СОСТАВ ОТВЕТА ────────────────────────────────────────────────────────
# ⚠️ Сверка самих белых списков с писаным договором живёт в БЫСТРОМ контуре
# (`tests/test_integration_whitelist_isolated.py`): она про состав списка,
# а не про содержимое ответа, и обязана краснеть даже там, где PostgreSQL
# нет вовсе. Здесь проверяется ВТОРАЯ половина — что наружу уехало ровно
# перечисленное.


@pytest.mark.asyncio
async def test_состав_ответа_по_отчёту_поимённо(клиент, db):
    await _основа(db)
    await _отчёт(db, id_=1)
    тело = (await клиент.get("/api/integration/reports/1", headers=ЗАГОЛОВОК)).json()
    assert set(тело) == ОЖИДАЕМЫЕ_ПОЛЯ_ОТЧЁТА | ДОБАВОЧНЫЕ_ОТЧЁТА, (
        "состав ответа по отчёту изменился: %s" % sorted(тело)
    )
    чек = тело["receipts"][0]
    assert set(чек) == ОЖИДАЕМЫЕ_ПОЛЯ_ЧЕКА | ДОБАВОЧНОЕ_ЧЕКА, (
        "состав ответа по чеку изменился: %s" % sorted(чек)
    )


@pytest.mark.asyncio
async def test_состав_ответа_в_списке_поимённо(клиент, db):
    """У списка свой состав, и он проверяется отдельно: две ручки — два ответа."""
    await _основа(db)
    await _отчёт(db, id_=1)
    тело = (await клиент.get(_список(), headers=ЗАГОЛОВОК)).json()
    assert set(тело) == {"reports", "next_cursor"}
    assert set(тело["reports"][0]) == ОЖИДАЕМЫЕ_ПОЛЯ_ОТЧЁТА, (
        "состав строки списка изменился: %s" % sorted(тело["reports"][0])
    )


@pytest.mark.asyncio
async def test_запретные_поля_не_уезжают_наружу(клиент, db):
    await _основа(db)
    await _отчёт(db, id_=1)
    ответ = await клиент.get("/api/integration/reports/1", headers=ЗАГОЛОВОК)
    тело = ответ.json()
    чек = тело["receipts"][0]
    for запретное in ЗАПРЕТНЫЕ:
        assert запретное not in тело, "в отчёт уехало поле %s" % запретное
        assert запретное not in чек, "в чек уехало поле %s" % запретное
    # ⚠️ И САМОГО СНИМКА НЕТ НИГДЕ В ОТВЕТЕ, даже вложенным значением: его
    # несёт `raw_data` целиком, и это главная находка аудита захода ⑤.
    assert СНИМОК not in ответ.text, "снимок уехал внутри значения"
    assert "receipts/900.jpg" not in ответ.text, "адрес в хранилище уехал наружу"


@pytest.mark.asyncio
async def test_распознанные_реквизиты_не_уезжают_а_настоящие_уезжают(клиент, db):
    """Заведомо разная пара: без неё «ocr_fd нет» неотличимо от «нет ничего»."""
    await _основа(db)
    await _отчёт(db, id_=1)
    await db.pool.execute("UPDATE receipts SET fd_num=$1 WHERE id=901", "555")
    чек = (await клиент.get("/api/integration/reports/1", headers=ЗАГОЛОВОК)).json()[
        "receipts"
    ][0]
    assert чек["fd_num"] == "555", "настоящий ФД не доехал"
    assert ОЦР_ФД not in str(чек), "распознанный ФД уехал наружу"
    assert ОЦР_ФПД not in str(чек), "распознанный ФПД уехал наружу"


@pytest.mark.asyncio
async def test_признак_снимка_есть_и_он_не_ссылка(клиент, db):
    await _основа(db)
    await _отчёт(db, id_=1)
    чек = (await клиент.get("/api/integration/reports/1", headers=ЗАГОЛОВОК)).json()[
        "receipts"
    ][0]
    assert чек["has_photo"] is True


@pytest.mark.asyncio
async def test_статья_расхода_приходит_словом_под_латинским_именем(клиент, db):
    await _основа(db)
    await db.pool.execute(
        "INSERT INTO category_groups (id, org_id, name, position) "
        "VALUES (1, 1, 'Транспорт', 1)"
    )
    await db.pool.execute(
        "INSERT INTO categories (id, org_id, group_id, name, tax_kind, position) "
        "VALUES (1, 1, 1, 'Топливо', 'Транспортные расходы', 1)"
    )
    await _отчёт(db, id_=1)
    await db.pool.execute("UPDATE receipts SET category_id=1 WHERE id=901")
    чек = (await клиент.get("/api/integration/reports/1", headers=ЗАГОЛОВОК)).json()[
        "receipts"
    ][0]
    assert чек["category_name"] == "Топливо"
    assert "статья" not in чек, "внутреннее имя псевдонима уехало наружу"


# ── ДОСТУП ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_без_ключа_401(клиент, db):
    await _основа(db)
    await _отчёт(db, id_=1)
    assert (await клиент.get(_список())).status_code == 401
    assert (await клиент.get("/api/integration/reports/1")).status_code == 401


@pytest.mark.asyncio
async def test_отозванный_ключ_перестаёт_пускать(клиент, db):
    """Отзыв обязан действовать НЕМЕДЛЕННО, иначе он не отзыв."""
    await _основа(db)
    await _отчёт(db, id_=1)
    assert (await клиент.get(_список(), headers=ЗАГОЛОВОК)).status_code == 200
    await db.pool.execute("UPDATE integration_keys SET revoked_at = NOW()")
    assert (await клиент.get(_список(), headers=ЗАГОЛОВОК)).status_code == 401


@pytest.mark.asyncio
async def test_ключ_видит_только_свою_организацию(клиент, db):
    """org-scope берётся ИЗ СТРОКИ КЛЮЧА, а не из запроса."""
    await _основа(db)
    await _отчёт(db, id_=1)
    await db.добавить_организацию(id=777, name="Чужая")
    await db.добавить_пользователя(id=9, first_name="Чужой", role="admin", org_id=777)
    await db.добавить_отчёт(
        id=500,
        title="Чужой",
        user_id=9,
        total=10,
        status="Одобрен",
        org_id=777,
        created=date(2026, 7, 1),
    )
    список = (await клиент.get(_список(), headers=ЗАГОЛОВОК)).json()
    assert [о["id"] for о in список["reports"]] == [1]
    assert (
        await клиент.get("/api/integration/reports/500", headers=ЗАГОЛОВОК)
    ).status_code == 404


@pytest.mark.asyncio
async def test_ключ_не_пускают_в_ручки_людей(клиент, db):
    """Права выражены МЕСТОМ: ключ открывает ровно то, где стоит его зависимость."""
    await _основа(db)
    await _отчёт(db, id_=1)
    # ⚠️ ПУТИ С КОСОЙ ЧЕРТОЙ. Без неё FastAPI отвечает 307 на канонический
    # адрес, и проверка «не пустил» оказалась бы зелёной, не дойдя до охраны.
    ручки_людей = (
        "/api/reports/",
        "/api/reports/1",
        "/api/reports/1/export.xlsx",
        "/api/receipts/",
        "/api/users/",
    )
    for путь in ручки_людей:
        ответ = await клиент.get(путь, headers=ЗАГОЛОВОК)
        assert ответ.status_code == 401, "%s пустил ключ интеграции" % путь


# ── ОТБОР ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_список_отдаёт_только_одобренные(клиент, db):
    await _основа(db)
    await _отчёт(db, id_=1, статус="Одобрен")
    await _отчёт(db, id_=2, статус="Черновик", с_чеком=False)
    await _отчёт(db, id_=3, статус="На проверке", с_чеком=False)
    ответ = await клиент.get(_список(), headers=ЗАГОЛОВОК)
    assert ответ.status_code == 200, ответ.text
    номера = [о["id"] for о in ответ.json()["reports"]]
    assert номера == [1], "в выдачу попали неодобренные отчёты: %s" % номера


@pytest.mark.asyncio
async def test_неодобренный_отчёт_неотличим_от_несуществующего(клиент, db):
    """Отдельный текст подтвердил бы, что отчёт с таким номером существует."""
    await _основа(db)
    await _отчёт(db, id_=1, статус="Черновик")
    черновик = await клиент.get("/api/integration/reports/1", headers=ЗАГОЛОВОК)
    чужой = await клиент.get("/api/integration/reports/999", headers=ЗАГОЛОВОК)
    assert черновик.status_code == чужой.status_code == 404
    assert черновик.json() == чужой.json()


@pytest.mark.asyncio
async def test_окно_дат_обязательно(клиент, db):
    """Без него первый же запрос забирает всю историю организации."""
    await _основа(db)
    ответ = await клиент.get("/api/integration/reports", headers=ЗАГОЛОВОК)
    assert ответ.status_code == 422, "окно дат оказалось необязательным"


@pytest.mark.asyncio
async def test_окно_дат_отсекает_и_обе_границы_включительно(клиент, db):
    await _основа(db)
    await _отчёт(db, id_=1, создан=date(2026, 6, 1), с_чеком=False)
    await _отчёт(db, id_=2, создан=date(2026, 7, 31), с_чеком=False)
    await _отчёт(db, id_=3, создан=date(2026, 8, 1), с_чеком=False)
    путь = "/api/integration/reports?date_from=2026-06-01&date_to=2026-07-31"
    номера = [
        о["id"] for о in (await клиент.get(путь, headers=ЗАГОЛОВОК)).json()["reports"]
    ]
    assert номера == [1, 2], "границы окна отработали не включительно: %s" % номера


@pytest.mark.asyncio
async def test_перевёрнутое_окно_даёт_400(клиент, db):
    await _основа(db)
    путь = "/api/integration/reports?date_from=2026-12-31&date_to=2026-01-01"
    assert (await клиент.get(путь, headers=ЗАГОЛОВОК)).status_code == 400


# ── СТРАНИЦЫ ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_курсор_не_теряет_и_не_двоит(клиент, db):
    """Три отчёта страницами по одному обязаны прийти ровно по разу."""
    await _основа(db)
    for n in (1, 2, 3):
        await _отчёт(db, id_=n, создан=date(2026, 7, n), с_чеком=False)
    собрано, курсор = [], None
    for _ in range(10):
        путь = _список(limit=1) + ("&cursor=" + курсор if курсор else "")
        тело = (await клиент.get(путь, headers=ЗАГОЛОВОК)).json()
        собрано += [о["id"] for о in тело["reports"]]
        курсор = тело["next_cursor"]
        if not курсор:
            break
    assert собрано == [1, 2, 3], "страницы потеряли или задвоили отчёты: %s" % собрано


@pytest.mark.asyncio
async def test_курсор_разводит_отчёты_одной_даты(клиент, db):
    """⚠️ ИМЕННО ПАРА «ДАТА, НОМЕР». Курсор по одной дате либо потерял бы
    соседей по дню, либо выдал бы их второй раз."""
    await _основа(db)
    for n in (1, 2, 3):
        await _отчёт(db, id_=n, создан=date(2026, 7, 1), с_чеком=False)
    собрано, курсор = [], None
    for _ in range(10):
        путь = _список(limit=1) + ("&cursor=" + курсор if курсор else "")
        тело = (await клиент.get(путь, headers=ЗАГОЛОВОК)).json()
        собрано += [о["id"] for о in тело["reports"]]
        курсор = тело["next_cursor"]
        if not курсор:
            break
    assert собрано == [1, 2, 3], "курсор не развёл отчёты одного дня: %s" % собрано


@pytest.mark.asyncio
async def test_последняя_страница_без_курсора(клиент, db):
    """Ровно полная страница НЕ должна выглядеть как «есть ещё»."""
    await _основа(db)
    for n in (1, 2):
        await _отчёт(db, id_=n, создан=date(2026, 7, n), с_чеком=False)
    тело = (await клиент.get(_список(limit=2), headers=ЗАГОЛОВОК)).json()
    assert len(тело["reports"]) == 2
    assert тело["next_cursor"] is None, "приёмник пошёл бы за пустой страницей"


@pytest.mark.asyncio
async def test_умолчание_страницы_режет_выдачу(клиент, db):
    """⚠️ БЕЗ `limit` РУЧКА ОТДАЁТ 100, А НЕ ВСЁ. Умолчание — это не удобство:
    именно его получит приёмник, который про страницы не знал и параметр
    не прислал. Заведено 101 — значит сотая граница видна, а не угадана.
    """
    await _основа(db)
    for n in range(1, 102):
        await _отчёт(db, id_=n, создан=date(2026, 7, 1), с_чеком=False)
    тело = (await клиент.get(_список(), headers=ЗАГОЛОВОК)).json()
    assert len(тело["reports"]) == инт.СТРАНИЦА_ПО_УМОЛЧАНИЮ == 100
    assert тело["next_cursor"] is not None, "остаток остался бы недостижимым"


@pytest.mark.asyncio
async def test_потолок_страницы(клиент, db):
    await _основа(db)
    assert (await клиент.get(_список(limit=1001), headers=ЗАГОЛОВОК)).status_code == 422


@pytest.mark.asyncio
async def test_негодный_курсор_даёт_400_а_не_первую_страницу(клиент, db):
    """Молча начав сначала, мы отдали бы приёмнику те же документы второй раз."""
    await _основа(db)
    ответ = await клиент.get(_список(cursor="мусор"), headers=ЗАГОЛОВОК)
    assert ответ.status_code == 400


# ── ФОРМА ЗНАЧЕНИЙ ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_даты_и_деньги_в_форме_которую_читает_1с(клиент, db):
    await _основа(db)
    await _отчёт(db, id_=1)
    тело = (await клиент.get("/api/integration/reports/1", headers=ЗАГОЛОВОК)).json()
    assert тело["created"] == "2026-07-01"
    assert тело["total"] == 100.50, "деньги приехали строкой, а не числом"
    чек = тело["receipts"][0]
    assert чек["date"] == "2026-06-11"
    # ⚠️ БЕЗ ЗОНЫ: это стенное время кассы, пояс ККТ неизвестен.
    assert чек["datetime"] == "2026-06-11T11:45:00"
    assert чек["amount"] == 100.50


@pytest.mark.asyncio
async def test_пустое_поле_приходит_пустым_а_не_пропадает(клиент, db):
    """Отсутствующий ключ приёмник прочтёт как «поля нет в договоре»."""
    await _основа(db)
    await _отчёт(db, id_=1)
    чек = (await клиент.get("/api/integration/reports/1", headers=ЗАГОЛОВОК)).json()[
        "receipts"
    ][0]
    assert "cashier" in чек and чек["cashier"] is None
