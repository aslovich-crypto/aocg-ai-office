# -*- coding: utf-8 -*-
"""Чужой адрес снимка качается только с названных заранее хостов (заход ⑥).

⚠️ ЗАЧЕМ ЭТА ПРОВЕРКА ВООБЩЕ СУЩЕСТВУЕТ. `receipts.photo_url` — колонка,
которую клиент задаёт САМ при заведении чека. Пока по этому адресу ходил
браузер человека (302), нас это не касалось. Ключу 302 не отдаётся, значит
по адресу из базы идёт НАШ сервер — и произвольная строка в колонке
превращается в запрос изнутри нашей сети: к внутренним службам, к служебным
адресам облака, к любому порту.

⚠️ ПРОВЕРЯЕТСЯ ИМЕННО ФУНКЦИЯ, А НЕ ОТВЕТ РУЧКИ. Ручка на запрещённый адрес
отвечает тем же 404, что и «снимка нет», — и это правильно снаружи, но по
ответу нельзя отличить «не пустили по хосту» от «в строке пусто». Предмет
здесь — само решение о допуске.
"""

import pytest

from app import receipt_photo
from app.storage import s3

ХРАНИЛИЩЕ = s3.S3Config(
    endpoint="https://s3.example.ru",
    bucket="aocg-receipts",
    region="ru-1",
    access_key="AKIDEXAMPLE",
    secret_key="not-a-secret-for-tests",
)


def test_адрес_хранилища_разрешён():
    assert receipt_photo._адрес_разрешён("https://s3.example.ru/a/b.jpg", ХРАНИЛИЩЕ)


@pytest.mark.parametrize(
    "адрес,почему",
    [
        ("http://s3.example.ru/a.jpg", "не https — трафик со снимком открыт"),
        ("https://r2.example/a.jpg", "чужой хост"),
        ("http://169.254.169.254/latest/meta-data/", "служебный адрес облака"),
        ("http://127.0.0.1:8000/admin", "своя же машина"),
        ("https://s3.example.ru.evil.net/a.jpg", "хост лишь НАЧИНАЕТСЯ как наш"),
        ("https://evil.net/?x=s3.example.ru", "наш хост стоит в строке запроса"),
        ("file:///etc/passwd", "вовсе не сеть"),
        ("", "пусто"),
    ],
)
def test_чужой_адрес_не_качается(адрес, почему):
    assert not receipt_photo._адрес_разрешён(адрес, ХРАНИЛИЩЕ), почему


def test_без_настроенного_хранилища_не_разрешено_ничего():
    """Настроек нет — значит и своего хоста нет, и список пуст."""
    assert not receipt_photo._адрес_разрешён("https://s3.example.ru/a.jpg", None)


def test_список_дополнительных_хостов_пуст_и_это_решение():
    """⚠️ ЗАМЕР ЕЩЁ НЕ СДЕЛАН, И ПОКА ОН НЕ СДЕЛАН — СПИСОК ПУСТ.

    Какие адреса реально лежат в `photo_url` на бою, знает только замер
    владельца. Вписать сюда хост по догадке значит открыть нашему серверу
    дорогу, которую мы не открывали осознанно. Проверка стоит, чтобы список
    не пополнили молча: покраснеет — значит кто-то назовёт основание.
    """
    assert receipt_photo.ДОПОЛНИТЕЛЬНЫЕ_ХОСТЫ_СНИМКОВ == ()


@pytest.mark.asyncio
async def test_за_перенаправлением_мы_не_идём(monkeypatch):
    """⚠️ ИНАЧЕ ПРОВЕРКА ХОСТА ОБХОДИТСЯ ОДНИМ ПЕРЕНАПРАВЛЕНИЕМ.

    Разрешённый хост отвечает 302 на внутренний адрес — и наш сервер послушно
    идёт туда, если клиенту велено следовать за перенаправлениями. Проверяется
    именно то, с какими настройками создан клиент, а не текст файла.
    """
    import httpx

    настройки = {}

    class Заглушка:
        def __init__(self, **kwargs):
            настройки.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, адрес):
            return httpx.Response(
                200, content=b"x", headers={"content-type": "image/png"}
            )

    monkeypatch.setattr(httpx, "AsyncClient", Заглушка)
    await receipt_photo._скачать("https://s3.example.ru/a.jpg", 1)
    assert настройки.get("follow_redirects") is False
    assert настройки.get("timeout") == receipt_photo.СРОК_ЧУЖОГО_АДРЕСА


@pytest.mark.asyncio
async def test_перенаправление_с_разрешённого_хоста_это_отказ(monkeypatch):
    """Вторая половина: 3xx обязан быть отказом, а не пустым успехом."""
    import httpx
    from fastapi import HTTPException

    class Заглушка:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, адрес):
            return httpx.Response(302, headers={"location": "http://127.0.0.1/"})

    monkeypatch.setattr(httpx, "AsyncClient", Заглушка)
    with pytest.raises(HTTPException) as отказ:
        await receipt_photo._скачать("https://s3.example.ru/a.jpg", 1)
    assert отказ.value.status_code == 404


def test_заголовок_снимка_один_и_тот_же_для_всех():
    """Разные значения означали бы разную чувствительность одного снимка."""
    assert receipt_photo.ЗАГОЛОВКИ_СНИМКА == {"Cache-Control": "no-store"}


def test_поля_снимка_названы_одним_списком():
    """Один список на человеческий и ключевой запрос: разойдись они по составу,
    одна из веток отдачи молча перестала бы находить свой источник."""
    assert receipt_photo.ПОЛЯ_СНИМКА == "photo_key, photo_url, raw_data"
