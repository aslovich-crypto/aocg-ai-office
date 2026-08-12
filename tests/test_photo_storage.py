"""Фото чека: три источника и стык между ними (задача №3, шаги 2–3).

ПОЧЕМУ ЭТОТ ФАЙЛ ОТДЕЛЬНЫЙ И ПОЧЕМУ ПОРЯДОК ИСТОЧНИКОВ ПРОВЕРЯЕТСЯ ЯВНО.
До конца S-40 старые и новые чеки живут ОДНОВРЕМЕННО: у старых снимок лежит
в базе строкой base64, у новых — в приватном бакете, и путь к нему хранится
ключом. Это стык, на котором ломается тихо: перевернётся порядок — часть
чеков начнёт отдавать устаревший источник, а снаружи это выглядит как
«фото просто другое», а не как ошибка. Требование проверить оба пути
и порядок между ними — владельца, 12.08.2026.

Порядок: photo_key → photo_url → raw_data.photo_base64.
"""

import base64
import io

import pytest

from app.storage import s3
from tests.test_api import _PNG_1x1, _install_fake, _Response

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode("ascii")


@pytest.fixture
def storage_env(monkeypatch):
    """Хранилище «настроено»: подпись считается, сеть не трогается."""
    monkeypatch.setenv("S3_ENDPOINT", "https://s3.example.ru")
    monkeypatch.setenv("S3_BUCKET", "aocg-receipts")
    monkeypatch.setenv("S3_REGION", "ru-1")
    monkeypatch.setenv("S3_ACCESS_KEY", "AKIDEXAMPLE")
    monkeypatch.setenv("S3_SECRET_KEY", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")


@pytest.fixture
def no_storage_env(monkeypatch):
    for name in (
        "S3_ENDPOINT",
        "S3_BUCKET",
        "S3_REGION",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


async def _create(client, **extra):
    payload = {
        "date": "2026-05-17",
        "org": "Магнит",
        "amount": 100.0,
        "source": "photo_ocr",
    }
    payload.update(extra)
    resp = await client.post("/api/receipts/", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ─────────────────────── порядок источников ───────────────────────


STORAGE_BYTES = b"\x89PNG-from-storage"


def _fake_storage(monkeypatch, content=STORAGE_BYTES, content_type="image/png"):
    """Хранилище отдаёт байты. Запоминаем, по какому ключу его спросили."""
    asked = {}

    async def fake_get(cfg, key, timeout=15.0):
        asked["key"] = key
        return content, content_type

    monkeypatch.setattr(s3, "get_object", fake_get)
    return asked


async def test_photo_key_is_proxied_and_signed_url_never_reaches_browser(
    client, storage_env, monkeypatch
):
    """Фото читает СЕРВЕР и отдаёт байтами. Ссылка наружу не уходит.

    Решение владельца 12.08.2026, довод дословно: подписанная ссылка — это
    КЛЮЧ ОТ ФАЙЛА. Ушла в браузер — попала в историю, журналы, закладки,
    пересланное сообщение, и пять минут чужой чек открывается вообще без
    входа в приложение. Мы везде остальное фильтруем по org_id — здесь
    нельзя иначе.
    """
    asked = _fake_storage(monkeypatch)
    created = await _create(client, photo_key="receipts/1/abc.jpg")
    assert created["photo_key"] == "receipts/1/abc.jpg"
    resp = await client.get(
        f"/api/receipts/{created['id']}/photo", follow_redirects=False
    )
    assert resp.status_code == 200
    assert resp.content == STORAGE_BYTES
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.headers.get("cache-control") == "no-store"
    assert asked["key"] == "receipts/1/abc.jpg"
    # Ни редиректа, ни адреса хранилища, ни подписи в ответе
    assert "location" not in resp.headers
    assert b"X-Amz-Signature" not in resp.content
    assert b"s3." not in resp.content


async def test_photo_missing_in_storage_is_404(client, storage_env, monkeypatch):
    async def not_found(cfg, key, timeout=15.0):
        raise s3.ObjectNotFound(key)

    monkeypatch.setattr(s3, "get_object", not_found)
    created = await _create(client, photo_key="receipts/1/gone.jpg")
    resp = await client.get(f"/api/receipts/{created['id']}/photo")
    assert resp.status_code == 404


async def test_storage_failure_is_502_and_leaks_nothing(
    client, storage_env, monkeypatch
):
    async def boom(cfg, key, timeout=15.0):
        raise s3.StorageError("хранилище отклонило чтение: HTTP 500 UnknownError")

    monkeypatch.setattr(s3, "get_object", boom)
    created = await _create(client, photo_key="receipts/1/abc.jpg")
    resp = await client.get(f"/api/receipts/{created['id']}/photo")
    assert resp.status_code == 502
    # 152-ФЗ: наружу не уходит ни ключ, ни адрес, ни ответ поставщика
    body = resp.text
    assert "receipts/1/abc.jpg" not in body
    assert "UnknownError" not in body and "s3." not in body


async def test_photo_url_alone_still_works(client, storage_env):
    # Старый путь не должен сломаться от появления нового.
    created = await _create(client, photo_url="https://r2.example/abc.jpg")
    resp = await client.get(
        f"/api/receipts/{created['id']}/photo", follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://r2.example/abc.jpg"


async def test_base64_alone_still_works(client, storage_env):
    # Чеки, снятые до №3, обязаны показываться как раньше — до конца S-40.
    created = await _create(client, raw_data={"photo_base64": _PNG_B64})
    resp = await client.get(f"/api/receipts/{created['id']}/photo")
    assert resp.status_code == 200
    assert resp.content == _PNG_BYTES


async def test_photo_key_wins_over_url_and_base64(client, storage_env, monkeypatch):
    asked = _fake_storage(monkeypatch)
    created = await _create(
        client,
        photo_key="receipts/1/new.jpg",
        photo_url="https://r2.example/old.jpg",
        raw_data={"photo_base64": _PNG_B64},
    )
    resp = await client.get(
        f"/api/receipts/{created['id']}/photo", follow_redirects=False
    )
    assert resp.status_code == 200
    # Байты РАЗНЫЕ у трёх источников — поэтому видно, который победил
    assert resp.content == STORAGE_BYTES, "ключ обязан побеждать адрес и base64"
    assert resp.content != _PNG_BYTES
    assert asked["key"] == "receipts/1/new.jpg"
    assert "location" not in resp.headers


async def test_photo_url_wins_over_base64_when_no_key(client, storage_env):
    created = await _create(
        client,
        photo_url="https://r2.example/old.jpg",
        raw_data={"photo_base64": _PNG_B64},
    )
    resp = await client.get(
        f"/api/receipts/{created['id']}/photo", follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://r2.example/old.jpg"


async def test_photo_key_without_configured_storage_is_503_not_404(
    client, no_storage_env
):
    """Ключ есть, а хранилище не настроено — это расхождение НАСТРОЕК.

    404 читался бы как «снимка нет», и его пошли бы искать в базе. Снимок
    при этом лежит в бакете и цел.
    """
    created = await _create(client, photo_key="receipts/1/abc.jpg")
    resp = await client.get(
        f"/api/receipts/{created['id']}/photo", follow_redirects=False
    )
    assert resp.status_code == 503


# ─────────────────────── ручка OCR: три исхода ───────────────────────


def _fake_ocr(monkeypatch):
    import json as _json

    payload = {"org_brand": "Магнит", "amount": 100.0, "confidence": "high"}
    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))


async def test_ocr_puts_photo_into_storage_and_stops_echoing_base64(
    client, monkeypatch, storage_env
):
    """Снимок уходит в бакет, а НЕ обратно в браузер.

    Раньше фотография ехала клиенту в base64 и оттуда же возвращалась
    в raw_data — то есть через браузер ДВАЖДЫ. Здесь круга больше нет.
    """
    _fake_ocr(monkeypatch)
    seen = {}

    async def fake_put(cfg, key, data, content_type="image/jpeg", timeout=15.0):
        seen["key"] = key
        seen["bytes"] = data
        return key

    monkeypatch.setattr(s3, "put_object", fake_put)

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    body = (await client.post("/api/receipts/ocr/", files=files)).json()

    assert body["photo_saved"] is True
    assert body["photo_key"] == seen["key"]
    assert body["photo_key"].startswith("receipts/")
    assert "photo_base64" not in body, "снимок больше не возвращается клиенту"
    assert seen["bytes"] == _PNG_1x1, "в хранилище ушёл ИМЕННО загруженный файл"


async def test_ocr_survives_storage_failure_and_says_so(
    client, db, monkeypatch, storage_env
):
    """ОТКАЗ ХРАНИЛИЩА: чек сохраняется всегда, фото вторично.

    Проверяем СОСТОЯНИЕ, а не код ответа: чек реально создан, photo_key пуст,
    и в ответе стоит честный признак «фото не сохранено». Запасного пути
    через base64 здесь нет НАМЕРЕННО — он вернул бы снимки в базу, ровно то,
    ради чего задача и затеяна.
    """
    _fake_ocr(monkeypatch)

    async def failing_put(*a, **kw):
        raise RuntimeError("хранилище недоступно")

    monkeypatch.setattr(s3, "put_object", failing_put)

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200, "отказ хранилища не должен ронять разбор чека"
    body = resp.json()
    assert body["photo_saved"] is False
    assert "photo_key" not in body
    assert "photo_base64" not in body, "запасного пути через базу здесь нет"
    assert body["amount"] == 100.0, "сам чек разобран и отдан"

    # и главное — чек СОХРАНЯЕТСЯ, а не теряется
    before = len(db.receipts)
    created = await _create(client, amount=body["amount"])
    assert len(db.receipts) == before + 1
    assert created["photo_key"] is None


async def test_ocr_keeps_base64_while_storage_is_not_configured(
    client, monkeypatch, no_storage_env
):
    """Переходный период: бакета ещё нет — работаем как раньше.

    Это не заглушка на будущее, а состояние прода на 12.08.2026: переменные
    хранилища в контейнере не заданы. Сломать этот путь значит сломать
    распознавание вживую.
    """
    _fake_ocr(monkeypatch)
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    body = (await client.post("/api/receipts/ocr/", files=files)).json()
    assert "photo_key" not in body
    assert body.get("photo_base64"), (
        "без хранилища снимок по-прежнему возвращается base64"
    )


# ─────────────────────── ключ объекта ───────────────────────


def test_object_key_carries_no_personal_data_and_is_unique():
    a = s3.build_object_key(7)
    b = s3.build_object_key(7)
    assert a.startswith("receipts/7/") and a.endswith(".jpg")
    assert a != b, "ключ обязан быть неугадываемым, а не последовательным"
    # В ключе нет ни даты покупки, ни ФИО, ни ИНН: он попадает в адрес
    # подписанной ссылки, а тот — в журналы и историю браузера.
    assert len(a.split("/")) == 3


# ─────────── S-48: удалённый чек не оставляет фото в хранилище ───────────


def _fake_delete(monkeypatch, fail=False):
    """Подмена удаления объекта. Копит ключи, по которым его звали."""
    calls = []

    async def fake(cfg, key, timeout=15.0):
        calls.append(key)
        if fail:
            raise s3.StorageError("хранилище отклонило удаление: HTTP 500")

    monkeypatch.setattr(s3, "delete_object", fake)
    return calls


async def test_deleting_receipt_removes_its_photo_from_storage(
    client, db, storage_env, monkeypatch
):
    """ЧЕК УДАЛЁН → ОБЪЕКТА В ХРАНИЛИЩЕ НЕТ.

    До №3 снимок лежал внутри чека и исчезал вместе со строкой. Вынеся его
    в бакет, мы САМИ создали бы состояние «пользователь удалил, а данные целы».
    Проверяется СОСТОЯНИЕМ: чека в базе нет И удаление объекта было вызвано
    ровно с его ключом.
    """
    calls = _fake_delete(monkeypatch)
    created = await _create(client, photo_key="receipts/1/dead.jpg")
    before = len(db.receipts)

    resp = await client.delete(f"/api/receipts/{created['id']}")
    assert resp.status_code == 200

    assert calls == ["receipts/1/dead.jpg"], "снимок обязан быть убран из хранилища"
    assert len(db.receipts) == before - 1
    assert all(r["id"] != created["id"] for r in db.receipts)


async def test_receipt_survives_when_storage_refuses_to_delete_photo(
    client, db, storage_env, monkeypatch
):
    """Хранилище не отдало снимок — ЧЕК НЕ УДАЛЯЕТСЯ.

    Обратный порядок оставлял бы в бакете персональные данные, на которые
    больше ничто не ссылается: невидимо ни в одном интерфейсе и не удалить.
    Лучше честный отказ и повтор.
    """
    _fake_delete(monkeypatch, fail=True)
    created = await _create(client, photo_key="receipts/1/stuck.jpg")
    before = len(db.receipts)

    resp = await client.delete(f"/api/receipts/{created['id']}")
    assert resp.status_code == 503

    assert len(db.receipts) == before, "чек обязан остаться, раз фото не убрано"
    assert any(r["id"] == created["id"] for r in db.receipts)


async def test_receipt_without_photo_deletes_without_touching_storage(
    client, db, storage_env, monkeypatch
):
    calls = _fake_delete(monkeypatch)
    created = await _create(client)
    resp = await client.delete(f"/api/receipts/{created['id']}")
    assert resp.status_code == 200
    assert calls == [], "без снимка хранилище дёргать незачем"


async def test_bulk_delete_removes_photos_and_reports_storage_blocks(
    client, db, storage_env, monkeypatch
):
    calls = _fake_delete(monkeypatch)
    a = await _create(client, photo_key="receipts/1/a.jpg", amount=111.0, org="Лента")
    b = await _create(client, amount=222.0, org="Ашан")
    body = {"ids": [a["id"], b["id"]]}

    resp = await client.post("/api/receipts/bulk-delete", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert set(data["deleted"]) == {a["id"], b["id"]}
    assert data["blocked_storage"] == []
    assert calls == ["receipts/1/a.jpg"]
    assert all(r["id"] not in (a["id"], b["id"]) for r in db.receipts)


async def test_bulk_delete_keeps_receipt_whose_photo_could_not_be_removed(
    client, db, storage_env, monkeypatch
):
    _fake_delete(monkeypatch, fail=True)
    a = await _create(client, photo_key="receipts/1/a.jpg", amount=333.0, org="Лента")
    b = await _create(client, amount=444.0, org="Ашан")

    resp = await client.post(
        "/api/receipts/bulk-delete", json={"ids": [a["id"], b["id"]]}
    )
    data = resp.json()
    assert data["blocked_storage"] == [a["id"]]
    assert data["deleted"] == [b["id"]], "чек без фото удаляется как обычно"
    # СОСТОЯНИЕ: чек со снимком остался, чек без снимка ушёл
    ids = [r["id"] for r in db.receipts]
    assert a["id"] in ids and b["id"] not in ids
