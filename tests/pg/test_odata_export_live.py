# -*- coding: utf-8 -*-
"""Ручки выгрузки отчёта в 1С и её отмены (1C-21, заход ③).

⚠️⚠️ ГРАНИЦА ПЕРВОЙ СТРОКОЙ: НАСТОЯЩАЯ 1С НЕ УЧАСТВУЕТ НИКОГДА. Клиент
подменяется функцией, как `s3.get_object` в `test_integration_photo_live.py`.
Зелёный прогон значит: гейт режет, журнал пишется, повтор отбивается,
частичный успех разобран. Он НЕ значит, что 1С примет документ, — это
выяснит приёмка на живой базе (заход ④) и только она.

⚠️ ЖИВОЙ КОНТУР — РАДИ ЖУРНАЛА И ИНДЕКСА. Частичный уникальный индекс
успешной выгрузки живёт в базе, и проверять его на двойнике значило бы
проверять зеркало.
"""

import pytest

from app import odata_client

ВЫГРУЗКА = "/api/odata/reports/%d/export"
ОТМЕНА = "/api/odata/reports/%d/cancel-export"


class ПоддельнаяОдинЭс:
    """Запоминает каждый вызов и отвечает ТАК, КАК ОТВЕЧАЕТ ПЛОЩАДКА 1С:ФРЕШ.

    ⚠️ ВЕДЁТ СЕБЯ КАК ЗАМЕРЕННАЯ ПЛОЩАДКА, А НЕ КАК УДОБНО ТЕСТУ (1C-21, ④):
    на любой `$filter` отвечает 500 с тем же текстом, что настоящая база, —
    «Операция не разрешена в предложении ГДЕ». Прежняя редакция этой
    поддельной 1С охотно фильтровала — и тесты были зелёными ровно там,
    где живая приёмка упала бы. Двойник, который умеет больше оригинала,
    прячет дефект, а не ловит его.

    Справочник на GET отдаётся ЦЕЛИКОМ: записи с `Ref_Key`, полем поиска
    и `DeletionMark`. `$select` соблюдается, как на площадке: поле, которое
    не попросили, не приходит. Иначе забытый в выборе `DeletionMark`
    доезжал бы в тест и удалённые отсеивались бы только здесь.

    `нечитаемый_ответ` — 200 с телом не в JSON (страница входа, прокси):
    клиент отдаёт тогда только размер, списка записей в ответе нет.
    """

    def __init__(
        self,
        проведение_ок=True,
        запись_код=200,
        контрагенты=None,
        счета=None,
        дубли=False,
        удалённые_контрагенты=None,
        удалённые_счета=None,
        сбой_чтения=None,
        нечитаемый_ответ=None,
    ):
        self.вызовы = []
        self.проведение_ок = проведение_ок
        self.запись_код = запись_код
        self.сбой_чтения = set(сбой_чтения or ())
        self.нечитаемый_ответ = set(нечитаемый_ответ or ())
        к = контрагенты if контрагенты is not None else {"7707083893": "k-guid"}
        с = счета if счета is not None else {"26": "s-26-guid"}
        self.записи_контрагентов = [
            {"Ref_Key": ref, "ИНН": инн, "DeletionMark": False}
            for инн, ref in к.items()
        ]
        if дубли:
            # Два живых контрагента с одним ИНН — замер нашёл 9 таких ИНН.
            self.записи_контрагентов += [
                {"Ref_Key": "второй", "ИНН": инн, "DeletionMark": False} for инн in к
            ]
        for инн in удалённые_контрагенты or ():
            self.записи_контрагентов.append(
                {"Ref_Key": "удалённый-" + инн, "ИНН": инн, "DeletionMark": True}
            )
        self.записи_счетов = [
            {"Ref_Key": ref, "Code": код, "DeletionMark": False}
            for код, ref in с.items()
        ]
        for код, ref in (удалённые_счета or {}).items():
            self.записи_счетов.append(
                {"Ref_Key": ref, "Code": код, "DeletionMark": True}
            )

    async def __call__(self, путь, *, метод="GET", параметры=None, тело=None):
        self.вызовы.append((метод, путь, параметры, тело))
        if метод == "GET":
            if "$filter" in (параметры or {}):
                return 500, {
                    "status": "odata_error",
                    "message": "Операция не разрешена в предложении ГДЕ",
                }
            if путь in self.сбой_чтения:
                return 500, {"status": "odata_error", "message": "сбой"}
            if путь in self.нечитаемый_ответ:
                return 200, {"тело": {"размер": 5120}}
            записи = (
                self.записи_контрагентов
                if "Контрагенты" in путь
                else self.записи_счетов
            )
            выбор = (параметры or {}).get("$select")
            if выбор:
                поля = выбор.split(",")
                записи = [{п: з for п, з in р.items() if п in поля} for р in записи]
            return 200, {"тело": {"value": записи}}
        if путь.endswith("/Post"):
            return (
                (200, {"тело": {}})
                if self.проведение_ок
                else (500, {"message": "не провелось"})
            )
        if self.запись_код != 200:
            return self.запись_код, {
                "status": "odata_error",
                "message": "1С ответила отказом",
            }
        return 200, {"тело": {"Ref_Key": "doc-guid", "Number": "0000-000010"}}


@pytest.fixture
def настроено(monkeypatch):
    for пер, знач in (
        ("ODATA_URL", "https://площадка/base/odata/standard.odata"),
        ("ODATA_LOGIN", "служебный"),
        ("ODATA_PASSWORD", "тайна"),
        ("ODATA_ORG_REF", "org-guid"),
        ("ODATA_WAREHOUSE_REF", "sklad-guid"),
        ("ODATA_CURRENCY_REF", "rub-guid"),
        ("ODATA_RESPONSIBLE_REF", "otv-guid"),
    ):
        monkeypatch.setenv(пер, знач)


async def _категория(db, имя="Такси и каршеринг"):
    """Категория отчёта. Заводится, а не ищется: пропуск «категорий нет»
    сделал бы сторожей молчаливо-зелёными (T87)."""
    номер = await db.pool.fetchval(
        "SELECT id FROM categories WHERE org_id=1 AND name=$1", имя
    )
    if номер is not None:
        return номер
    группа = await db.pool.fetchval(
        "SELECT id FROM category_groups WHERE org_id=1 LIMIT 1"
    ) or await db.pool.fetchval(
        "INSERT INTO category_groups (org_id, name, position)"
        " VALUES (1, 'Транспорт', 1) RETURNING id"
    )
    return await db.pool.fetchval(
        "INSERT INTO categories (org_id, group_id, name, tax_kind, position)"
        " VALUES (1, $1, $2, 'Прочие расходы', 1) RETURNING id",
        группа,
        имя,
    )


ВИД = "Прочие расходы"


async def _профиль(
    db,
    режим="usn_dr",
    ндс="included",
    счёт="26",
    форма="ooo",
    политика="when_mapped",
    совмещение=False,
):
    """Профиль учёта организации (AOCG-1C-001, § 5.1). Без него ручка 409."""
    await db.pool.execute(
        "INSERT INTO org_accounting_profile (org_id, legal_form, tax_regime,"
        " vat_mode, combines_psn, default_account_code, auto_post_policy)"
        " VALUES (1, $1, $2, $3, $4, $5, $6)"
        " ON CONFLICT (org_id) DO UPDATE SET legal_form=$1, tax_regime=$2,"
        " vat_mode=$3, combines_psn=$4, default_account_code=$5,"
        " auto_post_policy=$6",
        форма,
        режим,
        ндс,
        совмещение,
        счёт,
        политика,
    )


async def _правило_вида(db, вид=ВИД, счёт=None, статья="ст-вида", усн="Принимаются"):
    """Правило по ВИДУ расхода — основная ось сопоставления (§ 4)."""
    await db.pool.execute(
        "INSERT INTO org_expense_kind_map (org_id, tax_kind, account_code,"
        " expense_ref, expense_name, usn_reflection) VALUES (1, $1, $2, $3, $4, $5)"
        " ON CONFLICT (org_id, tax_kind) DO UPDATE SET account_code=$2,"
        " expense_ref=$3, usn_reflection=$5",
        вид,
        счёт,
        статья,
        "Статья вида",
        усн,
    )


async def _правило_категории(
    db, категория, счёт=None, статья="ст-категории", усн="Принимаются"
):
    """Переопределение по категории — исключение, оно главнее (§ 5.4)."""
    await db.pool.execute(
        "INSERT INTO org_category_map (org_id, category_id, account_code,"
        " expense_ref, expense_name, usn_reflection) VALUES (1, $1, $2, $3, $4, $5)"
        " ON CONFLICT (org_id, category_id) DO UPDATE SET account_code=$2,"
        " expense_ref=$3, usn_reflection=$5",
        категория,
        счёт,
        статья,
        "Статья категории",
        усн,
    )


async def _отчёт(
    db,
    статус="Одобрен",
    соответствие=True,
    инн="7707083893",
    правило=True,
    режим="usn_dr",
    ндс="included",
    счёт="26",
    усн="Принимаются",
    свод=None,
    политика="when_mapped",
    форма="ooo",
):
    """⚠️ ПРОФИЛЬ И ПРАВИЛО — ЧАСТЬ ОБЫЧНОЙ НАСТРОЙКИ (1C-22). Без профиля
    выгрузки нет вовсе, без правила категории документ не проводится."""
    await db.добавить_организацию(id=1, name="АОЦГ")
    await db.обеспечить_пользователя(id=1, first_name="Админ", role="admin")
    await db.добавить_отчёт(id=1, title="Июль", user_id=1, org_id=1, status=статус)
    from datetime import date

    категория = await _категория(db)
    await db.добавить_чек(
        id=1,
        org="Кофейня",
        amount=120,
        date=date(2026, 9, 1),
        org_id=1,
        user_id=1,
        category_id=категория,
    )
    await db.pool.execute("UPDATE receipts SET org_inn=$1 WHERE id=1", инн)
    if свод is not None:
        # ⚠️ СЛОВАРЬ, А НЕ СТРОКА JSON. У пула стоит кодек jsonb: строку он
        # закодировал бы ВТОРОЙ раз, и приложение прочитало бы текст вместо
        # свода — тест был бы зелёным, ничего не проверив.
        await db.pool.execute("UPDATE receipts SET vat_breakdown=$1 WHERE id=1", свод)
    await db.pool.execute(
        "INSERT INTO report_items (report_id, receipt_id) VALUES (1, 1)"
    )
    await _профиль(db, режим=режим, ндс=ндс, счёт=счёт, форма=форма, политика=политика)
    if правило:
        await _правило_вида(db, усн=усн)
    if соответствие:
        await db.pool.execute(
            "INSERT INTO odata_user_map (org_id, user_id, person_ref, person_name)"
            " VALUES (1, 1, 'person-guid', 'Админ')"
        )
    return категория


# ── ГЕЙТ И ДОПУСК ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_сотруднику_выгрузка_закрыта(client, as_role, db, настроено, monkeypatch):
    await _отчёт(db)
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    as_role("employee", user_id=1)
    assert (await client.post(ВЫГРУЗКА % 1)).status_code == 403


@pytest.mark.asyncio
async def test_неодобренный_отчёт_не_уезжает(client, db, настроено, monkeypatch):
    """Черновик в учёте клиента — документ, которого там быть не должно."""
    await _отчёт(db, статус="На проверке")
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 409 and "одобренные" in ответ.json()["detail"]
    assert одинэс.вызовы == [], "до проверки статуса ходили в 1С"


@pytest.mark.asyncio
async def test_без_соответствия_человека_отказ_а_не_догадка(
    client, db, настроено, monkeypatch
):
    """Решение 1C-08: подотчётное лицо сопоставляется справочником."""
    await _отчёт(db, соответствие=False)
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 409 and "соответствие" in ответ.json()["detail"]
    assert not any(в[0] == "POST" for в in одинэс.вызовы), "документ ушёл без человека"


# ── УСПЕХ И ПРЕДУПРЕЖДЕНИЕ ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_категория_без_соответствия_названа_и_держит_проведение(
    client, db, настроено, monkeypatch
):
    """⚠️⚠️ ГЛАВНОЕ ТРЕБОВАНИЕ 1C-22 (Р1 и Р3): категория без соответствия
    останавливает проведение и названа поимённо — и в ответе, и в журнале.
    Прежняя редакция проводила такой документ и лишь предупреждала; тогда
    «сработало» читалось как «разнесено верно»."""
    await _отчёт(db, правило=False)
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    тело = (await client.post(ВЫГРУЗКА % 1)).json()
    assert тело["выгружен"] is True and тело["проведён"] is False
    assert тело["категории_без_соответствия"] == ["Такси и каршеринг"]
    assert "Такси и каршеринг" in (тело["почему_не_проведён"] or "")
    assert "26" in тело["предупреждение"]
    записано = await db.pool.fetchrow(
        "SELECT outcome, doc_number, defaulted_categories FROM odata_exports"
        " WHERE report_id=1"
    )
    assert записано["outcome"] == "unposted"
    assert записано["doc_number"] == "0000-000010"
    assert записано["defaulted_categories"] == тело["категории_без_соответствия"], (
        "в журнале категории не те, что в ответе"
    )


@pytest.mark.asyncio
async def test_до_записи_ищутся_контрагент_и_счёт_чтением(
    client, db, настроено, monkeypatch
):
    """Контрагент и счёт едут ССЫЛКАМИ, найденными в базе клиента, а не кодом."""
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    await client.post(ВЫГРУЗКА % 1)
    чтения = [в[1] for в in одинэс.вызовы if в[0] == "GET"]
    assert "Catalog_Контрагенты" in чтения and "ChartOfAccounts_Хозрасчетный" in чтения
    запись = next(
        в for в in одинэс.вызовы if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    строка = запись[3]["Прочее"][0]
    assert строка["СчетЗатрат_Key"] == "s-26-guid", "в поле ссылки уехал код"
    assert строка["Поставщик_Key"] == "k-guid"


@pytest.mark.asyncio
async def test_счёта_нет_в_плане_счетов_документ_не_уходит(
    client, db, настроено, monkeypatch
):
    """⚠️ С ПАРОЙ (1C-21, ⑤): без неё тест был зелёным на коде, где план
    счетов не читался вовсе, — 409 приходил от сбоя `$filter`, а не от
    отсутствия счёта. Счёт появился в плане — документ обязан уехать."""
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс(счета={})
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 409 and "26" in ответ.json()["detail"]
    assert not any(в[0] == "POST" for в in одинэс.вызовы), (
        "документ ушёл со ссылкой в никуда"
    )

    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 200, (
        "счёт появился в плане, а выгрузка всё равно отбита — 409 выше был "
        "не про отсутствие счёта: %s" % ответ.text
    )


@pytest.mark.asyncio
async def test_контрагент_не_найден_называется_а_не_додумывается(
    client, db, настроено, monkeypatch
):
    await _отчёт(db, инн=None)
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    тело = (await client.post(ВЫГРУЗКА % 1)).json()
    assert тело["чеки_без_контрагента"] == [1]


# ── ПОВТОР И ОТМЕНА ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_повторная_выгрузка_отбивается_внятно_без_вызова_1с(
    client, db, настроено, monkeypatch
):
    """База запретит вторую успешную запись сама, но человек получил бы
    ошибку БАЗЫ. Отбиваем до вызова и объясняем, что делать."""
    await _отчёт(db)
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    await client.post(ВЫГРУЗКА % 1)
    второй = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", второй)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 409 and "уже выгружен" in ответ.json()["detail"]
    assert "0000-000010" in ответ.json()["detail"], "номер документа не назван"
    assert второй.вызовы == [], "при повторе ходили в 1С"


@pytest.mark.asyncio
async def test_отмена_выпускает_из_тупика(client, db, настроено, monkeypatch):
    """Требование 1C-23: документ в 1С удалили руками — отчёт не застревает."""
    await _отчёт(db)
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    await client.post(ВЫГРУЗКА % 1)
    отмена = await client.post(ОТМЕНА % 1)
    assert (
        отмена.status_code == 200
        and "удалите его в 1С сами" in отмена.json()["заметка"]
    )
    повтор = await client.post(ВЫГРУЗКА % 1)
    assert повтор.status_code == 200, (
        "после отмены выгрузка не прошла: %s" % повтор.text
    )
    исходы = [
        с["outcome"]
        for с in await db.pool.fetch(
            "SELECT outcome FROM odata_exports WHERE report_id=1 ORDER BY id"
        )
    ]
    assert исходы == ["cancelled", "ok"], "журнал не хранит историю: %s" % исходы


@pytest.mark.asyncio
async def test_сотруднику_отмена_закрыта(client, as_role, db, настроено, monkeypatch):
    await _отчёт(db)
    as_role("employee", user_id=1)
    assert (await client.post(ОТМЕНА % 1)).status_code == 403


# ── ЧАСТИЧНЫЙ УСПЕХ ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_частичный_успех_документ_не_удаляется_номер_отдаётся(
    client, db, настроено, monkeypatch
):
    """⚠️ РЕШЕНИЕ: удаление — ВТОРАЯ запись в чужую бухгалтерию, делать её
    молча в ответ на сбой опаснее самого сбоя. Документ остаётся
    НЕпроведённым, номер отдаётся человеку и пишется в журнал."""
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс(проведение_ок=False)
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    тело = (await client.post(ВЫГРУЗКА % 1)).json()
    assert тело["выгружен"] is True and тело["проведён"] is False
    assert тело["документ"] == "0000-000010"
    удаления = [в for в in одинэс.вызовы if в[0] in ("DELETE", "PATCH")]
    assert удаления == [], "при частичном успехе в 1С что-то удаляли"
    записано = await db.pool.fetchrow("SELECT outcome, doc_ref FROM odata_exports")
    assert записано["outcome"] == "partial" and записано["doc_ref"] == "doc-guid"


@pytest.mark.asyncio
async def test_после_частичного_успеха_второй_документ_не_создаётся(
    client, db, настроено, monkeypatch
):
    """⚠️ ИНДЕКС СТЕРЕЖЁТ ТОЛЬКО УСПЕХИ, а здесь документ создан при
    неуспехе. Защита от второго документа — в коде, и это проверяется."""
    await _отчёт(db)
    monkeypatch.setattr(
        odata_client, "запросить", ПоддельнаяОдинЭс(проведение_ок=False)
    )
    await client.post(ВЫГРУЗКА % 1)
    второй = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", второй)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 409 and "не проведён" in ответ.json()["detail"]
    assert второй.вызовы == [], "после частичного успеха ушёл второй документ"


@pytest.mark.asyncio
async def test_отказ_1с_пишется_в_журнал_и_не_роняет(
    client, db, настроено, monkeypatch
):
    await _отчёт(db)
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс(запись_код=400))
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 502
    исход = await db.pool.fetchval(
        "SELECT outcome FROM odata_exports WHERE report_id=1"
    )
    assert исход == "error"


# ── БЕЗ КОНТРАГЕНТА: ДОКУМЕНТ УХОДИТ, НО НЕ ПРОВОДИТСЯ (решение 16.09) ──


def _проводили(одинэс):
    return [в for в in одинэс.вызовы if в[0] == "POST" and в[1].endswith("/Post")]


async def _контрагент_не_держит(client, db, ожидаемые_чеки):
    """⚠️ ОТМЕНЁННОЕ ПРАВИЛО ОТ 16.09 (AOCG-1C-001, § 2.2). Контрагент нужен
    только чтобы зарегистрировать счёт-фактуру, а по чеку ККТ её нет вовсе —
    значит вычета нет и С контрагентом. Документ проводится, чеки названы."""
    тело = (await client.post(ВЫГРУЗКА % 1)).json()
    assert тело["проведён"] is True, "контрагент удержал проведение: %s" % тело
    assert тело["чеки_без_контрагента"] == ожидаемые_чеки, "чеки не названы"
    assert тело["почему_не_проведён"] is None
    строка = await db.pool.fetchrow("SELECT outcome FROM odata_exports")
    assert строка["outcome"] == "ok"


@pytest.mark.asyncio
async def test_нет_инн_при_вычете_документ_всё_равно_проводится(
    client, db, настроено, monkeypatch
):
    """Даже там, где НДС принимается к вычету: счёта-фактуры по чеку ККТ нет,
    и контрагент вычета не добавит."""
    await _отчёт(db, инн=None, ндс="deductible")
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    await _контрагент_не_держит(client, db, [1])


@pytest.mark.asyncio
async def test_нет_инн_при_ндс_в_стоимости_документ_проводится(
    client, db, настроено, monkeypatch
):
    """⚠️ ПРАВИЛО ОТ 16.09 ОТМЕНЕНО (Р1). Контрагент держит проведение
    только там, где он нужен учёту, — при вычете НДС."""
    await _отчёт(db, инн=None, ндс="included")
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    await _контрагент_не_держит(client, db, [1])


@pytest.mark.asyncio
async def test_инн_без_совпадения_документ_проводится(
    client, db, настроено, monkeypatch
):
    """Второй путь: ИНН есть, контрагента с ним в базе клиента нет."""
    await _отчёт(db, ндс="deductible")
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс(контрагенты={}))
    await _контрагент_не_держит(client, db, [1])


@pytest.mark.asyncio
async def test_два_совпадения_по_инн_поставщик_не_ставится(
    client, db, настроено, monkeypatch
):
    """Третий путь: выбрать «первого из двух» и есть додумывание. Поставщик
    не ставится, но документ проводится — держать его незачем."""
    await _отчёт(db, ндс="deductible")
    одинэс = ПоддельнаяОдинЭс(дубли=True)
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    await _контрагент_не_держит(client, db, [1])
    запись = next(
        в for в in одинэс.вызовы if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    assert "Поставщик_Key" not in запись[3]["Прочее"][0], "уехал первый из двух"


@pytest.mark.asyncio
async def test_нужный_контрагент_не_первый_в_справочнике(
    client, db, настроено, monkeypatch
):
    """⚠️ ВТОРОЙ СТОРОЖ НА ⑳ (требование владельца 17.09.2026). Во всех
    прежних тестах в справочнике стоял один контрагент, и первая запись
    совпадала с нужной. Замер: код, который ИНН не сверяет и берёт первую
    запись справочника, проходил все 29 тестов. Здесь нужный — третий,
    перед ним два живых с чужими ИНН, и уехать обязана ссылка третьего."""
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс(
        контрагенты={
            "1111111111": "k-чужой-первый",
            "2222222222": "k-чужой-второй",
            "7707083893": "k-нужный-третий",
        }
    )
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    тело = (await client.post(ВЫГРУЗКА % 1)).json()
    assert тело["чеки_без_контрагента"] == [] and тело["проведён"] is True, тело
    запись = next(
        в for в in одинэс.вызовы if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    assert запись[3]["Прочее"][0]["Поставщик_Key"] == "k-нужный-третий", (
        "уехал не тот контрагент: %s" % запись[3]["Прочее"][0]["Поставщик_Key"]
    )


@pytest.mark.asyncio
async def test_с_контрагентом_документ_проводится(client, db, настроено, monkeypatch):
    """Пара к трём тестам выше: правило не должно было отнять проведение
    у отчётов, где контрагенты найдены у всех."""
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    тело = (await client.post(ВЫГРУЗКА % 1)).json()
    assert тело["проведён"] is True and len(_проводили(одинэс)) == 1
    assert тело["почему_не_проведён"] is None


@pytest.mark.asyncio
async def test_после_непроведённого_второй_документ_не_уходит(
    client, db, настроено, monkeypatch
):
    await _отчёт(db, правило=False)
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    await client.post(ВЫГРУЗКА % 1)
    второй = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", второй)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 409 and "не проведён" in ответ.json()["detail"]
    assert второй.вызовы == [], "после непроведённого ушёл второй документ"


@pytest.mark.asyncio
async def test_непроведённую_выгрузку_можно_отменить(
    client, db, настроено, monkeypatch
):
    await _отчёт(db, правило=False)
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    await client.post(ВЫГРУЗКА % 1)
    assert (await client.post(ОТМЕНА % 1)).status_code == 200


# ── ПЕРЕМЕННЫЕ: БЕЗ НИХ ВЫГРУЗКА ОТВЕЧАЕТ 503 С ИМЕНАМИ ──────────────────


@pytest.mark.asyncio
async def test_без_единой_odata_переменной_ручка_503_с_именами(client, db, monkeypatch):
    """⚠️ Отсутствие ссылок на 1С ломает ВЫГРУЗКУ, а не приложение: ручка
    отвечает 503 и перечисляет, чего не хватает, — по именам, без значений."""
    for пер in (
        "ODATA_URL",
        "ODATA_LOGIN",
        "ODATA_PASSWORD",
        "ODATA_ORG_REF",
        "ODATA_WAREHOUSE_REF",
        "ODATA_CURRENCY_REF",
        "ODATA_RESPONSIBLE_REF",
    ):
        monkeypatch.delenv(пер, raising=False)
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 503, ответ.text
    текст = ответ.json()["detail"]
    for имя in (
        "ODATA_ORG_REF",
        "ODATA_WAREHOUSE_REF",
        "ODATA_CURRENCY_REF",
        "ODATA_RESPONSIBLE_REF",
    ):
        assert имя in текст, "не названа %s: %s" % (имя, текст)
    assert одинэс.вызовы == [], "при ненастроенном обмене ходили в 1С"


# ── ЧТЕНИЕ СПРАВОЧНИКОВ БЕЗ $filter (1C-21, заход ⑤) ─────────────────────


def _чтения(одинэс):
    return [в for в in одинэс.вызовы if в[0] == "GET"]


@pytest.mark.asyncio
async def test_в_1с_не_уходит_ни_одного_filter(client, db, настроено, monkeypatch):
    """⚠️ ГЛАВНЫЙ СТОРОЖ ЗАХОДА. На площадке `$filter` не работает вовсе —
    девять проб дали 500. Возврат фильтра в любой запрос убивает выгрузку
    на живой базе при зелёных тестах, если двойник его охотно исполняет."""
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 200, ответ.text
    с_фильтром = [в[1] for в in одинэс.вызовы if "$filter" in (в[2] or {})]
    assert с_фильтром == [], "в 1С ушёл $filter: %s" % с_фильтром


@pytest.mark.asyncio
async def test_справочники_читаются_с_узким_select(client, db, настроено, monkeypatch):
    """Без выбора полей контрагенты весят 5.8 МБ, с тремя полями — 88 КБ.
    DeletionMark обязателен: без него удалённые не отсеять."""
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    await client.post(ВЫГРУЗКА % 1)
    выборы = {в[1]: (в[2] or {}).get("$select", "") for в in _чтения(одинэс)}
    assert выборы.get("Catalog_Контрагенты") == "Ref_Key,ИНН,DeletionMark", выборы
    assert выборы.get("ChartOfAccounts_Хозрасчетный") == "Ref_Key,Code,DeletionMark", (
        выборы
    )


@pytest.mark.asyncio
async def test_удалённый_контрагент_не_даёт_ложного_дубля(
    client, db, настроено, monkeypatch
):
    """⚠️ Живой + помеченный на удаление с тем же ИНН — это ОДИН контрагент.
    Без отсева вышло бы «два совпадения», и документ не провёлся бы зря."""
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс(удалённые_контрагенты=["7707083893"])
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    тело = (await client.post(ВЫГРУЗКА % 1)).json()
    assert тело["чеки_без_контрагента"] == [], (
        "удалённый контрагент сломал сопоставление"
    )
    assert тело["проведён"] is True
    запись = next(
        в for в in одинэс.вызовы if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    assert запись[3]["Прочее"][0]["Поставщик_Key"] == "k-guid", (
        "уехала ссылка на удалённого"
    )


@pytest.mark.asyncio
async def test_только_удалённый_контрагент_не_считается_найденным(
    client, db, настроено, monkeypatch
):
    await _отчёт(db, ндс="deductible")
    одинэс = ПоддельнаяОдинЭс(контрагенты={}, удалённые_контрагенты=["7707083893"])
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    тело = (await client.post(ВЫГРУЗКА % 1)).json()
    assert тело["чеки_без_контрагента"] == [1] and тело["проведён"] is True


@pytest.mark.asyncio
async def test_удалённый_счёт_не_находится_и_документ_не_уходит(
    client, db, настроено, monkeypatch
):
    """⚠️ ПАРНАЯ ПРОВЕРКА ОБЯЗАТЕЛЬНА. 409 «нет 20.01» даёт и отсев
    удалённого, и сбой чтения, выданный за «не нашлось». Первая редакция
    без пары была зелёной на коде ДО правки: там `$filter` падал на 500,
    и отказ приходил по ложной причине. Поэтому тот же счёт живым обязан
    найтись и уехать — только так 409 выше означает именно отсев."""
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс(счета={}, удалённые_счета={"26": "s-удалённый"})
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 409 and "26" in ответ.json()["detail"]
    assert not any(в[0] == "POST" for в in одинэс.вызовы), (
        "документ ушёл на удалённый счёт"
    )

    живой = ПоддельнаяОдинЭс(счета={"26": "s-живой"})
    monkeypatch.setattr(odata_client, "запросить", живой)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 200, (
        "тот же счёт живым не нашёлся — 409 выше был не отсевом: %s" % ответ.text
    )
    запись = next(
        в for в in живой.вызовы if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    assert запись[3]["Прочее"][0]["СчетЗатрат_Key"] == "s-живой"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "справочник, слова",
    [
        ("ChartOfAccounts_Хозрасчетный", "план счетов"),
        ("Catalog_Контрагенты", "справочник контрагентов"),
    ],
)
async def test_сбой_чтения_справочника_это_503_а_не_409(
    client, db, настроено, monkeypatch, справочник, слова
):
    """⚠️ РЕШЕНИЕ ВЛАДЕЛЬЦА 17.09.2026: «не прочиталось» и «не нашлось» —
    разные беды. Раньше сбой чтения превращался в пустой словарь, и человек
    читал «в плане счетов нет 20.01», хотя счёт в базе есть."""
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс(сбой_чтения=[справочник])
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 503, "сбой чтения выдан как %s: %s" % (
        ответ.status_code,
        ответ.text,
    )
    assert слова in ответ.json()["detail"]
    assert not any(в[0] == "POST" for в in одинэс.вызовы), (
        "после сбоя чтения ушёл документ"
    )
    записей = await db.pool.fetchval("SELECT count(*) FROM odata_exports")
    assert записей == 0, "сбой чтения оставил запись в журнале выгрузок"


@pytest.mark.asyncio
async def test_ответ_200_без_списка_записей_это_503(client, db, настроено, monkeypatch):
    """Второй путь сбоя чтения: 1С ответила 200, но не справочником —
    страницей входа или ответом прокси. Пустой словарь здесь дал бы то же
    ложное «в плане счетов нет 20.01»."""
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс(нечитаемый_ответ=["ChartOfAccounts_Хозрасчетный"])
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 503, "нечитаемый ответ выдан как %s: %s" % (
        ответ.status_code,
        ответ.text,
    )
    assert "план счетов" in ответ.json()["detail"]
    assert not any(в[0] == "POST" for в in одинэс.вызовы)
    assert await db.pool.fetchval("SELECT count(*) FROM odata_exports") == 0


@pytest.mark.asyncio
async def test_без_инн_контрагенты_не_читаются(client, db, настроено, monkeypatch):
    """Нет ни одного ИНН — 88 КБ контрагентов читать незачем."""
    await _отчёт(db, инн=None)
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    await client.post(ВЫГРУЗКА % 1)
    assert "Catalog_Контрагенты" not in [в[1] for в in _чтения(одинэс)]


# ── 1C-22: ПРОФИЛЬ ОРГАНИЗАЦИИ ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_без_профиля_выгрузки_нет_и_в_1с_не_ходим(
    client, db, настроено, monkeypatch
):
    """⚠️ Без профиля неизвестны режим НДС и счёт по умолчанию. Собирать
    документ на догадках нельзя, поэтому отказ ДО чтения справочников,
    до журнала и до записи."""
    await _отчёт(db)
    await db.pool.execute("DELETE FROM org_accounting_profile WHERE org_id=1")
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 409, ответ.text
    assert "профиль" in ответ.json()["detail"].lower()
    assert одинэс.вызовы == [], "без профиля всё равно сходили в 1С"
    assert await db.pool.fetchval("SELECT count(*) FROM odata_exports") == 0


@pytest.mark.asyncio
async def test_счёт_по_умолчанию_берётся_из_профиля(client, db, настроено, monkeypatch):
    """⚠️ Р2. Константы «20.01» в коде больше нет: счёт задаёт профиль,
    и в плане счетов ищется именно он."""
    await _отчёт(db, правило=False, счёт="44")
    одинэс = ПоддельнаяОдинЭс(счета={"44": "s-44-guid"})
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 200, ответ.text
    запись = next(
        в for в in одинэс.вызовы if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    assert запись[3]["Прочее"][0]["СчетЗатрат_Key"] == "s-44-guid"


@pytest.mark.asyncio
async def test_отражение_в_усн_уезжает_из_соответствия(
    client, db, настроено, monkeypatch
):
    """⚠️ Р4. Значение берётся у правила категории, а не пишется константой."""
    await _отчёт(db, усн="НеПринимаются")
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    assert (await client.post(ВЫГРУЗКА % 1)).status_code == 200
    запись = next(
        в for в in одинэс.вызовы if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    assert запись[3]["Прочее"][0]["ОтражениеВУСН"] == "НеПринимаются"


@pytest.mark.asyncio
async def test_на_осно_поле_усн_в_1с_не_уходит(client, db, настроено, monkeypatch):
    """Организация на общем режиме: поля упрощёнки в её документе быть
    не должно вовсе."""
    await _отчёт(db, режим="osno", ндс="deductible")
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    assert (await client.post(ВЫГРУЗКА % 1)).status_code == 200
    запись = next(
        в for в in одинэс.вызовы if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    assert "ОтражениеВУСН" not in запись[3]["Прочее"][0]


@pytest.mark.asyncio
async def test_при_ндс_в_стоимости_чек_уезжает_одной_строкой(
    client, db, настроено, monkeypatch
):
    """Свод из двух ставок, а строка одна: разносить НДС, который к вычету
    не принимается, значит давать бухгалтеру лишнюю сверку."""
    await _отчёт(db, ндс="included", свод={"20": 10.0, "10": 5.0})
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    assert (await client.post(ВЫГРУЗКА % 1)).status_code == 200
    запись = next(
        в for в in одинэс.вызовы if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    строки = запись[3]["Прочее"]
    assert len(строки) == 1, "чек раздробили вне режима вычета: %s" % строки
    assert строки[0]["СтавкаНДС"] == "БезНДС"
    assert строки[0]["Сумма"] == 120.0


@pytest.mark.asyncio
async def test_при_вычете_тот_же_чек_дробится_по_ставкам(
    client, db, настроено, monkeypatch
):
    """Пара к тесту выше на тех же данных: режим НДС решает, а не свод."""
    await _отчёт(db, ндс="deductible", свод={"20": 10.0, "10": 5.0})
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    assert (await client.post(ВЫГРУЗКА % 1)).status_code == 200
    запись = next(
        в for в in одинэс.вызовы if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    строки = запись[3]["Прочее"]
    assert len(строки) == 3, "при вычете чек не раздроблен: %s" % строки
    assert round(sum(с["Сумма"] for с in строки), 2) == 120.0


# ── 1C-22 v2: ОСЬ ВИДОВ РАСХОДА И ПОЛИТИКА ПРОВЕДЕНИЯ ────────────────────


@pytest.mark.asyncio
async def test_правило_вида_расхода_проводит_документ(
    client, db, настроено, monkeypatch
):
    """Девять строк настройки вместо сорока восьми: правило по виду
    расхода покрывает категорию без отдельной строки для неё."""
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    тело = (await client.post(ВЫГРУЗКА % 1)).json()
    assert тело["проведён"] is True and тело["категории_без_соответствия"] == []
    запись = next(
        в for в in одинэс.вызовы if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    assert запись[3]["Прочее"][0]["Субконто2"] == "ст-вида"


@pytest.mark.asyncio
async def test_правило_категории_главнее_правила_вида(
    client, db, настроено, monkeypatch
):
    """§ 5.4: исключение по категории обязано побеждать общее правило."""
    категория = await _отчёт(db)
    await _правило_категории(db, категория, счёт="26")
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    assert (await client.post(ВЫГРУЗКА % 1)).status_code == 200
    запись = next(
        в for в in одинэс.вызовы if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    assert запись[3]["Прочее"][0]["Субконто2"] == "ст-категории"


@pytest.mark.asyncio
async def test_политика_никогда_не_проводит(client, db, настроено, monkeypatch):
    """§ 7: клиент может решить, что проводит только его бухгалтер."""
    await _отчёт(db, политика="never")
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    тело = (await client.post(ВЫГРУЗКА % 1)).json()
    assert тело["проведён"] is False and тело["политика_проведения"] == "never"
    assert тело["категории_без_соответствия"] == []
    assert not any(в[1].endswith("/Post") for в in одинэс.вызовы), "вызов Post ушёл"
    assert await db.pool.fetchval("SELECT outcome FROM odata_exports") == "unposted"


@pytest.mark.asyncio
async def test_политика_всегда_проводит_даже_без_соответствия(
    client, db, настроено, monkeypatch
):
    """§ 7: «always» оставлено для клиентов, которые правят в 1С сами.
    Документ проводится, но категория всё равно названа."""
    await _отчёт(db, правило=False, политика="always")
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    тело = (await client.post(ВЫГРУЗКА % 1)).json()
    assert тело["проведён"] is True
    assert тело["категории_без_соответствия"] == ["Такси и каршеринг"]


@pytest.mark.asyncio
async def test_на_доходной_усн_признак_непринимаются(
    client, db, настроено, monkeypatch
):
    """§ 2.1: на УСН «Доходы» расходы налог не уменьшают никогда."""
    await _отчёт(db, режим="usn_d")
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    assert (await client.post(ВЫГРУЗКА % 1)).status_code == 200
    запись = next(
        в for в in одинэс.вызовы if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    assert запись[3]["Прочее"][0]["ОтражениеВУСН"] == "НеПринимаются"


@pytest.mark.asyncio
async def test_содержание_и_комментарий_уезжают_в_1с(
    client, db, настроено, monkeypatch
):
    """Свободные поля — состав задан владельцем 17.09.2026."""
    await _отчёт(db)
    await db.pool.execute(
        "UPDATE receipts SET fd_num='4321', fpd='788765239' WHERE id=1"
    )
    одинэс = ПоддельнаяОдинЭс()
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    assert (await client.post(ВЫГРУЗКА % 1)).status_code == 200
    тело_документа = next(
        в[3]
        for в in одинэс.вызовы
        if в[0] == "POST" and в[1] == "Document_АвансовыйОтчет"
    )
    assert тело_документа["Прочее"][0]["Содержание"] == (
        "Кофейня · ФД 4321 · ФПД 788765239 · Такси и каршеринг"
    )
    комментарий = тело_документа["Комментарий"]
    assert комментарий.startswith("AOCG AI Офис · отчёт №1 · ")
    assert "token" not in комментарий.lower()
