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
    """Запоминает каждый вызов и отвечает по сценарию.

    ⚠️ ЧТЕНИЯ СПРАВОЧНИКОВ И ЗАПИСЬ РАЗЛИЧАЮТСЯ ПО МЕТОДУ: так проверяется,
    что до POST ручка сходила за контрагентами и счетами, а не додумала их.
    """

    def __init__(
        self, проведение_ок=True, запись_код=200, контрагенты=None, счета=None
    ):
        self.вызовы = []
        self.проведение_ок = проведение_ок
        self.запись_код = запись_код
        self.контрагенты = (
            контрагенты if контрагенты is not None else {"7707083893": "k-guid"}
        )
        self.счета = счета if счета is not None else {"20.01": "s-2001-guid"}

    async def __call__(self, путь, *, метод="GET", параметры=None, тело=None):
        self.вызовы.append((метод, путь, параметры, тело))
        if метод == "GET":
            фильтр = (параметры or {}).get("$filter", "")
            значение = фильтр.split("'")[1] if "'" in фильтр else ""
            словарь = self.контрагенты if "Контрагенты" in путь else self.счета
            ссылка = словарь.get(значение)
            return 200, {"тело": {"value": [{"Ref_Key": ссылка}] if ссылка else []}}
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


async def _отчёт(db, статус="Одобрен", соответствие=True, инн="7707083893"):
    await db.добавить_организацию(id=1, name="АОЦГ")
    await db.обеспечить_пользователя(id=1, first_name="Админ", role="admin")
    await db.добавить_отчёт(id=1, title="Июль", user_id=1, org_id=1, status=статус)
    from datetime import date

    await db.добавить_чек(
        id=1, org="Кофейня", amount=120, date=date(2026, 9, 1), org_id=1, user_id=1
    )
    await db.pool.execute("UPDATE receipts SET org_inn=$1 WHERE id=1", инн)
    await db.pool.execute(
        "INSERT INTO report_items (report_id, receipt_id) VALUES (1, 1)"
    )
    if соответствие:
        await db.pool.execute(
            "INSERT INTO odata_user_map (org_id, user_id, person_ref, person_name)"
            " VALUES (1, 1, 'person-guid', 'Админ')"
        )


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
async def test_ответ_говорит_о_счёте_по_умолчанию_и_называет_категории(
    client, db, настроено, monkeypatch
):
    """⚠️⚠️ ГЛАВНОЕ ТРЕБОВАНИЕ ЗАХОДА: «сработало» не должно читаться
    как «разнесено верно». Правил нет — ответ обязан это сказать."""
    await _отчёт(db)
    monkeypatch.setattr(odata_client, "запросить", ПоддельнаяОдинЭс())
    тело = (await client.post(ВЫГРУЗКА % 1)).json()
    assert тело["выгружен"] is True and тело["проведён"] is True
    assert тело["счета_по_умолчанию"], "подстановка не названа"
    assert "20.01" in тело["предупреждение"]
    записано = await db.pool.fetchrow(
        "SELECT outcome, doc_number, defaulted_categories FROM odata_exports WHERE report_id=1"
    )
    assert записано["outcome"] == "ok" and записано["doc_number"] == "0000-000010"
    assert записано["defaulted_categories"] == тело["счета_по_умолчанию"], (
        "в журнале подстановки не те, что в ответе"
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
    assert строка["СчетЗатрат_Key"] == "s-2001-guid", "в поле ссылки уехал код"
    assert строка["Поставщик_Key"] == "k-guid"


@pytest.mark.asyncio
async def test_счёта_нет_в_плане_счетов_документ_не_уходит(
    client, db, настроено, monkeypatch
):
    await _отчёт(db)
    одинэс = ПоддельнаяОдинЭс(счета={})
    monkeypatch.setattr(odata_client, "запросить", одинэс)
    ответ = await client.post(ВЫГРУЗКА % 1)
    assert ответ.status_code == 409 and "20.01" in ответ.json()["detail"]
    assert not any(в[0] == "POST" for в in одинэс.вызовы), (
        "документ ушёл со ссылкой в никуда"
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
