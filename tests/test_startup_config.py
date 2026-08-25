# -*- coding: utf-8 -*-
"""Отказ старта при негодном APP_URL (T62).

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ НА САМОМ ДЕЛЕ. Не «функция умеет бросать исключение» —
это дёшево и почти бесполезно, — а ЧТО ОНА ВООБЩЕ ВЫЗЫВАЕТСЯ ПРИ СТАРТЕ.
Сторож, которого забыли позвать, выглядит в коде точно так же, как рабочий:
файл на месте, тест функции зелёный, приложение молча поднимается с пустым
адресом. Поэтому главный тест здесь — не про функцию, а про lifespan.

ПОЧЕМУ НЕ ЛОМАЮТСЯ ОСТАЛЬНЫЕ ТЕСТЫ. Остальной набор ходит через
httpx.ASGITransport, который lifespan НЕ выполняет, — приложение в них
собирается, но не «стартует». Отсюда и цена варианта: проверка при старте
не мешает ни тестам, ни инструментам замера, в отличие от отказа на импорте
(как у JWT_SECRET_KEY), который свалил бы четыре инструмента и шесть
документированных команд запуска.
"""

import importlib.util
import pathlib

import pytest

import app.main as main
import app.routers.auth as auth


async def _пусто(*_a, **_k):
    """Заглушка init_db: база в этих тестах не нужна и не должна трогаться."""
    return None


async def _поднять_приложение():
    """Пройти lifespan целиком, как это делает настоящий сервер."""
    async with main.app.router.lifespan_context(main.app):
        pass


# ─── ГЛАВНОЕ: проверка действительно висит на старте ───


async def test_старт_отказывает_при_пустом_APP_URL(monkeypatch):
    """Ловит удаление вызова из lifespan — самую вероятную будущую поломку."""
    monkeypatch.setattr(main, "init_db", _пусто)
    monkeypatch.setattr(auth, "APP_URL", "")
    with pytest.raises(RuntimeError, match="APP_URL не задан"):
        await _поднять_приложение()


async def test_старт_проходит_при_верном_APP_URL(monkeypatch):
    """Вторая половина: сторож обязан ПРОПУСКАТЬ здоровую настройку.

    Без этой проверки «падает всегда» неотличимо от «падает по делу».
    """
    monkeypatch.setattr(main, "init_db", _пусто)
    monkeypatch.setattr(auth, "APP_URL", "https://app.aocgai.ru")
    await _поднять_приложение()


async def test_проверка_идёт_ДО_базы(monkeypatch):
    """Негодная настройка обязана останавливать раньше, чем что-то заработает.

    Если проверку переставить после init_db, приложение успеет открыть пул
    к боевой базе на настройке, с которой всё равно не полетит.
    """
    трогали = []

    async def _след(*_a, **_k):
        трогали.append("init_db")

    monkeypatch.setattr(main, "init_db", _след)
    monkeypatch.setattr(auth, "APP_URL", "")
    with pytest.raises(RuntimeError):
        await _поднять_приложение()
    assert трогали == [], "init_db вызван до проверки настройки"


# ─── сама проверка ───


def test_адрес_без_схемы_отвергается(monkeypatch):
    """`app.aocgai.ru` в письме выглядит правдоподобно и не открывается."""
    monkeypatch.setattr(auth, "APP_URL", "app.aocgai.ru")
    with pytest.raises(RuntimeError, match="нет схемы"):
        auth.проверить_адрес_фронта()


def test_проверяется_константа_а_не_окружение(monkeypatch):
    """⚠️ Разделяющая проверка: окружение здоровое, константа пуста.

    Перечитывание os.getenv внутри сторожа сделало бы этот тест зелёным,
    хотя в письма ушёл бы пустой адрес: в ссылки подставляется КОНСТАНТА,
    прочитанная на импорте. Проверять надо ровно то значение, которое уйдёт.
    """
    monkeypatch.setenv("APP_URL", "https://app.aocgai.ru")
    monkeypatch.setattr(auth, "APP_URL", "")
    # ⚠️ СЛИЧАЕТСЯ ТЕКСТ ОТКАЗА, А НЕ ПРОСТО ФАКТ ОТКАЗА. Без этого мутант
    # «сторож перечитывает окружение» проходил мимо теста, названного в его
    # честь: пустая константа всё равно спотыкалась о проверку схемы ниже,
    # исключение вылетало — и тест зеленел по чужой причине. Красный тест
    # обязан краснеть за то, что назван (T11).
    with pytest.raises(RuntimeError, match="APP_URL не задан"):
        auth.проверить_адрес_фронта()


def test_завершающая_косая_снимается_при_чтении():
    """Иначе ссылка склеится в app.aocgai.ru//verify-email.

    Модуль загружается ОТДЕЛЬНЫМ экземпляром, а не перезагружается на месте:
    reload подменил бы константу у всех остальных тестов прогона, и падение
    вылезло бы в чужом файле.
    """
    путь = pathlib.Path(auth.__file__)
    spec = importlib.util.spec_from_file_location("auth_копия_для_замера", путь)
    копия = importlib.util.module_from_spec(spec)
    import os

    было = os.environ.get("APP_URL")
    os.environ["APP_URL"] = "https://app.aocgai.ru/"
    try:
        spec.loader.exec_module(копия)
    finally:
        if было is None:
            os.environ.pop("APP_URL", None)
        else:
            os.environ["APP_URL"] = было
    assert копия.APP_URL == "https://app.aocgai.ru"


def test_мёртвого_railway_умолчания_больше_нет(monkeypatch):
    """Прямая проверка того, ради чего задача заводилась.

    Переменная не задана — константа пуста, а не подставляет старый адрес.
    """
    путь = pathlib.Path(auth.__file__)
    spec = importlib.util.spec_from_file_location("auth_копия_без_env", путь)
    копия = importlib.util.module_from_spec(spec)
    import os

    было = os.environ.pop("APP_URL", None)
    try:
        spec.loader.exec_module(копия)
    finally:
        if было is not None:
            os.environ["APP_URL"] = было
    assert копия.APP_URL == ""
