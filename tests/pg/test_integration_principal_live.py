# -*- coding: utf-8 -*-
"""Зависимость проверки ключа — на живой базе (шаг 3, заход ④).

⚠️ ЖИВАЯ БАЗА, А НЕ ДВОЙНИК, И ПО СУЩЕСТВУ. Предмет проверки — что именно
отобрал SQL: отозванный и истёкший ключи отсекаются условием ЗАПРОСА, а не
кодом на питоне. На двойнике это условие пришлось бы изображать, то есть
проверять зеркало, а не приложение.

⚠️ И ВТОРОЕ, ЧТО ПРОВЕРЯЕТСЯ ТОЛЬКО ЗДЕСЬ: срок сверяется с часами БАЗЫ
(`expires_at > NOW()`). Расхождение часов контейнера дало бы ключ, который
для одного инстанса ещё жив, а для другого уже мёртв.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app import integration_keys as ик

ПРЕФИКС = "aocg-proba-1"
СЕКРЕТ = "не-секрет-а-проба"
ПОЛНЫЙ = ПРЕФИКС + "." + СЕКРЕТ


async def _завести(db, *, префикс=ПРЕФИКС, секрет=СЕКРЕТ, отозван=None, истекает=None):
    await db.добавить_организацию(id=1)
    await db.добавить_пользователя(id=1, first_name="А", role="admin", org_id=1)
    await db.pool.execute(
        "INSERT INTO integration_keys "
        "(org_id, prefix, secret_hash, name, created_by, revoked_at, expires_at) "
        "VALUES (1, $1, $2, '1С бюро', 1, $3, $4)",
        префикс,
        ик.хеш_секрета(секрет),
        отозван,
        истекает,
    )


@pytest.mark.asyncio
async def test_живой_ключ_даёт_принципала_с_организацией(db):
    await _завести(db)
    строка = await ик.найти_живой(db.pool, ПРЕФИКС)
    assert строка is not None, "живой ключ не найден"
    п = ик.принципал(строка)
    assert п["org_id"] == 1
    assert п["role"] == "integration"
    assert п["key_prefix"] == ПРЕФИКС
    assert "secret_hash" not in п


@pytest.mark.asyncio
async def test_отозванный_ключ_не_находится(db):
    """Отзыв обязан работать НЕМЕДЛЕННО — иначе он не отзыв."""
    await _завести(db, отозван=datetime.now(timezone.utc))
    assert await ик.найти_живой(db.pool, ПРЕФИКС) is None


@pytest.mark.asyncio
async def test_истёкший_ключ_не_находится(db):
    await _завести(db, истекает=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert await ик.найти_живой(db.pool, ПРЕФИКС) is None


@pytest.mark.asyncio
async def test_ключ_с_будущим_сроком_живой(db):
    """Заведомо разная пара к проверке выше: срок в будущем НЕ отсекает.

    Без этой половины «истёкший не находится» неотличимо от «не находится
    ничего».
    """
    await _завести(db, истекает=datetime.now(timezone.utc) + timedelta(days=1))
    assert await ик.найти_живой(db.pool, ПРЕФИКС) is not None


@pytest.mark.asyncio
async def test_верный_ключ_проходит_зависимость(db):
    await _завести(db)
    п = await ик.get_integration_principal(ПОЛНЫЙ)
    assert п["org_id"] == 1 and п["role"] == "integration"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "значение,почему",
    [
        (None, "заголовка нет вовсе"),
        ("", "заголовок пуст"),
        ("мусор", "нет разделителя"),
        (ПРЕФИКС + ".неверный", "секрет не сошёлся"),
        ("aocg-proba-9." + СЕКРЕТ, "префикса нет в базе"),
    ],
)
async def test_негодный_ключ_даёт_401_без_подробностей(db, значение, почему):
    """⚠️ ОТКАЗ ОДИН НА ВСЕ СЛУЧАИ, И ТЕКСТ ОДИН.

    Разные тексты рассказали бы подбирающему, на каком шаге он остановился:
    «отозван» подтверждает, что префикс существовал.
    """
    await _завести(db)
    with pytest.raises(HTTPException) as отказ:
        await ик.get_integration_principal(значение)
    assert отказ.value.status_code == 401, почему
    assert отказ.value.detail == "Неверный ключ интеграции", почему


@pytest.mark.asyncio
async def test_отозванный_ключ_даёт_тот_же_отказ_что_и_несуществующий(db):
    """Ответ не должен различать «был и отозван» и «не было никогда»."""
    await _завести(db, отозван=datetime.now(timezone.utc))
    with pytest.raises(HTTPException) as отозванный:
        await ик.get_integration_principal(ПОЛНЫЙ)
    with pytest.raises(HTTPException) as чужой:
        await ик.get_integration_principal("aocg-proba-9." + СЕКРЕТ)
    assert отозванный.value.detail == чужой.value.detail
    assert отозванный.value.status_code == чужой.value.status_code


@pytest.mark.asyncio
async def test_ключ_чужой_организации_несёт_свою_организацию(db):
    """org-scope у ключа берётся ИЗ ЕГО СТРОКИ, а не из запроса."""
    await _завести(db)
    await db.добавить_организацию(id=777, name="Вторая")
    await db.добавить_пользователя(id=9, first_name="Б", role="admin", org_id=777)
    await db.pool.execute(
        "INSERT INTO integration_keys "
        "(org_id, prefix, secret_hash, name, created_by) "
        "VALUES (777, $1, $2, 'вторая', 9)",
        "aocg-proba-2",
        ик.хеш_секрета(СЕКРЕТ),
    )
    первый = await ик.get_integration_principal(ПОЛНЫЙ)
    второй = await ик.get_integration_principal("aocg-proba-2." + СЕКРЕТ)
    assert первый["org_id"] == 1 and второй["org_id"] == 777
