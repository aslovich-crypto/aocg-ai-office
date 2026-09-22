# -*- coding: utf-8 -*-
"""Клиент OData: состав запроса, повтор и молчание про доступы (1C-21, ①).

⚠️⚠️ ГРАНИЦА ЭТИХ ТЕСТОВ, НАЗВАНА ПЕРВОЙ СТРОКОЙ, ЧТОБЫ ЗЕЛЁНЫЙ ПРОГОН
НЕ ЧИТАЛСЯ ШИРЕ, ЧЕМ ОН ЕСТЬ: **зелёный тест здесь НЕ означает, что запись
авансового отчёта в 1С работает.** Он означает ровно две вещи — мы шлём
то, что собирались, и разбираем то, что пришло. Настоящая 1С в тестах
не участвует НИКОГДА: сеть подменена, как подменяется `s3.get_object`
в `tests/pg/test_integration_photo_live.py`.

⚠️ ПОЧЕМУ ПОДМЕНА ФУНКЦИИ, А НЕ СЕТЕВАЯ ЗАГЛУШКА. В проекте нет ни одной
библиотеки-заглушки HTTP, и заводить её ради одного клиента — новая
зависимость под одну задачу. Приём подменой уже применён к хранилищу
и к почте, он знаком и читается без документации.
"""

import asyncio

import httpx
import pytest

from app import odata_client as од


class ПоддельныйОтвет:
    def __init__(self, код=200, тело=None, текст=b"<xml/>", заголовки=None):
        self.status_code = код
        self._тело = тело
        self.content = текст
        self.headers = заголовки or {"DataServiceVersion": "3.0"}

    def json(self):
        if self._тело is None:
            raise ValueError("не json")
        return self._тело


class ПоддельныйКлиент:
    """Запоминает, ЧТО мы послали, и отдаёт заранее назначенные ответы."""

    def __init__(self, ответы):
        self.ответы = list(ответы)
        self.вызовы = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def request(self, метод, адрес, **прочее):
        self.вызовы.append({"метод": метод, "адрес": адрес, **прочее})
        исход = self.ответы.pop(0)
        if isinstance(исход, Exception):
            raise исход
        return исход


@pytest.fixture
def доступы(monkeypatch):
    monkeypatch.setenv(
        "ODATA_URL", "https://площадка.example/base/odata/standard.odata/"
    )
    monkeypatch.setenv("ODATA_LOGIN", "служебный")
    monkeypatch.setenv("ODATA_PASSWORD", "тайна")


def подменить(monkeypatch, клиент):
    # ⚠️ НАСТОЯЩИЙ `sleep` СОХРАНЯЕТСЯ ДО ПОДМЕНЫ, И ЭТО НЕ ПРИДИРКА:
    # `од.asyncio` — тот же самый модуль `asyncio`, что и здесь, поэтому
    # лямбда, зовущая `asyncio.sleep`, позвала бы САМУ СЕБЯ. Первая редакция
    # так и сделала и получила RecursionError вместо проверки повтора.
    настоящий_sleep = asyncio.sleep
    monkeypatch.setattr(од.httpx, "AsyncClient", lambda **_: клиент)
    monkeypatch.setattr(од.asyncio, "sleep", lambda _: настоящий_sleep(0))


# ── СОСТАВ ЗАПРОСА ───────────────────────────────────────────────────────


def test_шлём_туда_куда_собирались(monkeypatch, доступы):
    к = ПоддельныйКлиент([ПоддельныйОтвет()])
    подменить(monkeypatch, к)
    код, _ = asyncio.run(од.запросить("$metadata"))
    assert код == 200
    вызов = к.вызовы[0]
    assert вызов["метод"] == "GET"
    # ⚠️ ДВОЙНОЙ КОСОЙ ЧЕРТЫ БЫТЬ НЕ ДОЛЖНО: адрес в переменной окружения
    # человек напишет и со слешем на конце, и без — склейка обязана это снести.
    assert вызов["адрес"].endswith("/standard.odata/$metadata")
    assert "//standard" not in вызов["адрес"].replace("https://", "")
    assert вызов["auth"] == ("служебный", "тайна")
    assert вызов["headers"]["Accept"] == "application/json"


def test_версия_интерфейса_берётся_из_заголовка(monkeypatch, доступы):
    """Замер 1C-20: схема приходит XML-ом, версия — заголовком."""
    подменить(monkeypatch, ПоддельныйКлиент([ПоддельныйОтвет()]))
    итог = asyncio.run(од.проверить_связь())
    assert итог["дошли"] is True and итог["версия"] == "3.0"


def test_xml_в_ответе_не_считается_поломкой(monkeypatch, доступы):
    """`$metadata` не JSON, и это устройство OData, а не отказ."""
    подменить(monkeypatch, ПоддельныйКлиент([ПоддельныйОтвет(текст=b"<edmx/>")]))
    код, тело = asyncio.run(од.запросить("$metadata"))
    assert код == 200 and тело["тело"] == {"размер": len(b"<edmx/>")}


# ── ПОВТОР ───────────────────────────────────────────────────────────────


def test_повтор_на_обрыве_связи(monkeypatch, доступы):
    к = ПоддельныйКлиент([httpx.ConnectError("нет связи"), ПоддельныйОтвет()])
    подменить(monkeypatch, к)
    код, _ = asyncio.run(од.запросить("$metadata"))
    assert код == 200 and len(к.вызовы) == 2, "вторая попытка не сделана"


def test_ответ_с_кодом_ошибки_НЕ_повторяется(monkeypatch, доступы):
    """⚠️ ГЛАВНОЕ ПРАВИЛО КЛИЕНТА. Получен HTTP-код — чужая система нас
    услышала. Повтор такого запроса при записи создал бы ВТОРОЙ документ."""
    к = ПоддельныйКлиент([ПоддельныйОтвет(код=500)])
    подменить(monkeypatch, к)
    код, тело = asyncio.run(од.запросить("$metadata"))
    assert len(к.вызовы) == 1, "запрос повторён после ответа сервера"
    assert код == 500 and тело["status"] == "odata_error"


def test_две_неудачи_дают_503_и_не_роняют(monkeypatch, доступы):
    к = ПоддельныйКлиент([httpx.ConnectTimeout("таймаут"), httpx.ConnectError("нет")])
    подменить(monkeypatch, к)
    код, тело = asyncio.run(од.запросить("$metadata"))
    assert код == 503 and тело["status"] == "odata_unavailable"
    assert len(к.вызовы) == 2


# ── ДОСТУПЫ И МОЛЧАНИЕ ПРО НИХ ───────────────────────────────────────────


def test_без_доступов_503_а_не_падение(monkeypatch):
    for имя in ("ODATA_URL", "ODATA_LOGIN", "ODATA_PASSWORD"):
        monkeypatch.delenv(имя, raising=False)
    код, тело = asyncio.run(од.запросить("$metadata"))
    assert код == 503 and тело["status"] == "odata_not_configured"
    assert not од.настроено()


def test_пароль_не_попадает_в_текст_отказа(monkeypatch, доступы):
    # Отказ про ненастроенность называет ИМЕНА переменных, а не значения.
    monkeypatch.delenv("ODATA_PASSWORD", raising=False)
    _, тело = asyncio.run(од.запросить("$metadata"))
    сообщение = тело["message"]
    assert "ODATA_PASSWORD" in сообщение
    assert "тайна" not in сообщение and "служебный" not in сообщение


@pytest.mark.parametrize(
    "адрес",
    [
        "https://msk1.example.com/a/hs/base12345/odata/standard.odata/$metadata",
        "https://служебный:тайна@msk1.example.com/base/odata",
    ],
)
def test_в_журнал_не_уходят_ни_хост_ни_учётка(адрес):
    """⚠️ По имени хоста видно, ЧЬЮ бухгалтерию мы дёргаем."""
    вышло = од.безопасный_адрес(адрес)
    assert "msk1.example.com" not in вышло
    assert "тайна" not in вышло and "служебный" not in вышло
    assert вышло.startswith("https://…/")


def test_пароль_маскируется_в_журнальном_словаре():
    """Ключ `odata_password` под общее правило «password» не подпадал:
    сравнение идёт по точному имени ключа."""
    from aocg_security.masking import mask_log_dict

    вышло = mask_log_dict(
        {
            "odata_login": "служебный",
            "odata_password": "тайна",
            "odata_url": "https://x",
        }
    )
    assert вышло == {"odata_login": "***", "odata_password": "***", "odata_url": "***"}


def test_запись_НЕ_повторяется_даже_на_обрыве_связи(monkeypatch, доступы):
    """⚠️⚠️ ДЫРА, НАЙДЕННАЯ МУТАЦИЕЙ ⑬. Правило повтора стало строже в заходе ③:
    при ЗАПИСИ повтора нет вовсе. Оборванный POST мог ДОЙТИ и создать документ,
    а ответ потеряться; повтор завёл бы второй авансовый отчёт в учёте клиента.
    Тестов на это не было — мутант «повторять и запись» выжил."""
    к = ПоддельныйКлиент([httpx.ConnectError("обрыв"), ПоддельныйОтвет()])
    подменить(monkeypatch, к)
    код, тело = asyncio.run(
        од.запросить("Document_АвансовыйОтчет", метод="POST", тело={"a": 1})
    )
    assert len(к.вызовы) == 1, "запись повторена после обрыва: второй документ"
    assert код == 503 and тело["status"] == "odata_unavailable"


def test_чтение_на_обрыве_повторяется_по_прежнему(monkeypatch, доступы):
    """Пара к тесту выше: ужесточение не должно было отнять повтор у чтения."""
    к = ПоддельныйКлиент([httpx.ConnectError("обрыв"), ПоддельныйОтвет()])
    подменить(monkeypatch, к)
    код, _ = asyncio.run(од.запросить("Catalog_Контрагенты"))
    assert код == 200 and len(к.вызовы) == 2


# ── РАЗРЕШЁННЫЕ МЕТОДЫ (1C-29 В6) ──────────────────────────────────────────


def _взрыв(**_):
    raise AssertionError("запрос дошёл до сети — запрещённый метод не остановлен")


@pytest.mark.parametrize("метод", ["DELETE", "delete", "PUT", "MERGE"])
def test_запрещённый_метод_отвергается_до_сети(monkeypatch, доступы, метод):
    """⚠️⚠️ В OData 1С DELETE удаляет документ ФИЗИЧЕСКИ, без пометки
    (CLAUDE.md, 1C-30). Отказ обязан случиться ДО сети: сеть подменена
    взрывом, и ValueError вместо AssertionError значит, что до неё не дошли.
    Строчная `delete` — чтобы список не обходился регистром."""
    monkeypatch.setattr(од.httpx, "AsyncClient", _взрыв)
    with pytest.raises(ValueError, match="запрещён"):
        asyncio.run(од.запросить("Document_АвансовыйОтчет(guid'x')", метод=метод))


def test_запрещённый_метод_отвергается_и_без_доступов(monkeypatch):
    """Проверка стоит ДО чтения доступов: без переменных окружения прежний
    путь ответил бы 503 «не настроено», и DELETE выглядел бы как штатный
    отказ обмена, а не как ошибка программиста."""
    for пер in ("ODATA_URL", "ODATA_LOGIN", "ODATA_PASSWORD"):
        monkeypatch.delenv(пер, raising=False)
    with pytest.raises(ValueError, match="запрещён"):
        asyncio.run(од.запросить("x", метод="DELETE"))


@pytest.mark.parametrize("метод", ["GET", "POST", "PATCH"])
def test_разрешённые_методы_уходят_в_сеть(monkeypatch, доступы, метод):
    """Положительная половина: список не режет то, чем обмен живёт.
    PATCH — единственный законный способ пометить документ на удаление."""
    к = ПоддельныйКлиент([ПоддельныйОтвет(тело={})])
    подменить(monkeypatch, к)
    код, _ = asyncio.run(од.запросить("x", метод=метод))
    assert код == 200 and к.вызовы[0]["метод"] == метод
