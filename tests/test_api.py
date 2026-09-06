"""API endpoint tests for AOCG AI Office.

Run against an in-memory fake pool (see conftest.py) — no real database is
touched. Each test gets a fresh store via the `db` / `seeded` fixtures.

⚠️ ФАЙЛ ПЕРЕЕЗЖАЕТ НА ЖИВУЮ БАЗУ (T36, перевод по заходам).

06.09.2026, ЗАХОД 1 — сняты ручки ЧЕКОВ (61 тест): список, создание, четыре
ветки дедупа, предупреждения, разбор raw_data в колонки и позиции, PATCH,
DELETE, массовое удаление, подсказка оплаты, источник и фото. Живут в
`tests/pg/test_receipts_api.py`.

06.09.2026, ЗАХОД 2 — сняты ручки ОТЧЁТОВ (60 тестов): создание и смена
статуса, контракт форм ответа, удаление по статусам, автор, кто утверждает,
видимость по ролям, состав, производный `total`, скоуп receiptIds по орг.
Живут в `tests/pg/test_reports_api.py`.

06.09.2026, ЗАХОД 3 — сняты КАРТЫ и ЖУРНАЛ СОГЛАСИЙ (10 тестов). Живут в
`tests/pg/test_cards_consent_api.py`.

⚠️ ЧТО ОСТАЁТСЯ ЗДЕСЬ НАВСЕГДА И ПОЧЕМУ — ЧТОБЫ ЧЕРЕЗ МЕСЯЦ НЕ СОЧЛИ ЗАБЫТЫМ.
Пятнадцать тестов ручки распознавания (`POST /api/receipts/ocr/`) на живую базу
НЕ переводятся — решение владельца 06.09.2026. Ручка разбирает ответ модели:
клиент Anthropic подменён целиком, ручка ничего не пишет и ничего не читает,
база ей нужна только потому, что её требует фикстура клиента. Живой контур
стоит подъёма кластера на прогон; платить его за проверку разбора JSON
незачем. Это НЕ остаток перевода — это его граница.
"""

from datetime import date


# ─── POST /api/receipts/ocr/ ──────────────────────────────────────────
# A 1×1 PNG — anything we'd actually OCR is too big to inline, and the
# Anthropic client is mocked end-to-end so the image bytes never reach it.
import base64
import io

from anthropic import APITimeoutError

import app.routers.ocr as ocr_module

_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class _Block:
    """Minimal stand-in for an Anthropic text content block."""

    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeMessages:
    def __init__(self, behavior):
        self._behavior = behavior

    async def create(self, **kwargs):
        return self._behavior(kwargs)


class _FakeClient:
    """Stand-in for AsyncAnthropic.with_options(...) result."""

    def __init__(self, behavior):
        self.messages = _FakeMessages(behavior)


def _install_fake(monkeypatch, behavior):
    """Replace the module-level Anthropic client with one that runs `behavior`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    class _Stub:
        def with_options(self, **_):
            return _FakeClient(behavior)

    monkeypatch.setattr(ocr_module, "_anthropic_client", _Stub())


async def test_ocr_rejects_non_image(client):
    files = {"file": ("note.txt", io.BytesIO(b"hello"), "text/plain")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 400
    assert "Unsupported" in resp.json()["detail"]


async def test_ocr_rejects_oversized_file(client):
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 10)
    files = {"file": ("big.png", io.BytesIO(big), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 400
    assert "exceeds" in resp.json()["detail"].lower()


async def test_ocr_rejects_empty_file(client):
    files = {"file": ("empty.png", io.BytesIO(b""), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 400


async def test_ocr_happy_path(client, monkeypatch):
    payload = {
        "org_legal": 'ООО "Тандер"',
        "org_brand": "Магнит",
        "org_inn": "7707083893",
        "address": "Москва",
        "datetime": "2026-05-15T13:42:00",
        "amount": 1234.56,
        "operation_type": "purchase",
        "payment_form": "card",
        "tax_system": "usn_income",
        "vat_20": 123.45,
        "items": [
            {
                "position": 1,
                "name": "Молоко",
                "quantity": 1,
                "price": 89.0,
                "sum": 89.0,
                "vat_rate": "20",
            }
        ],
        "confidence": "high",
    }
    import json as _json

    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["org_brand"] == "Магнит"
    assert body["org"] == "Магнит"  # alias: org_brand or org_legal
    assert body["amount"] == 1234.56
    # auto-categorization v2 picks up "Магнит" → "Продукты для офиса"
    assert body["category"] == "Продукты для офиса"


async def test_ocr_strips_markdown_fences(client, monkeypatch):
    """Claude sometimes wraps JSON in ```json ... ``` despite the prompt."""
    wrapped = (
        '```json\n{"org_brand": "Лукойл", "amount": 3000, "confidence": "medium"}\n```'
    )
    _install_fake(monkeypatch, lambda kw: _Response(wrapped))

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["org"] == "Лукойл"
    assert body["category"] == "Топливо"


async def test_ocr_timeout_returns_low_confidence(client, monkeypatch):
    def boom(_kw):
        raise APITimeoutError(request=None)

    _install_fake(monkeypatch, boom)

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    # User said: timeout / unreadable -> low-confidence object, NOT 500.
    assert resp.status_code == 200
    body = resp.json()
    assert body["confidence"] == "low"
    assert body["amount"] is None
    assert body["org"] is None


async def test_ocr_garbage_response_returns_low_confidence(client, monkeypatch):
    _install_fake(monkeypatch, lambda kw: _Response("sorry, I cannot read this"))

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200
    assert resp.json()["confidence"] == "low"


async def test_ocr_missing_api_key_returns_low_confidence(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Don't install a fake — we shouldn't reach the client at all.
    monkeypatch.setattr(ocr_module, "_anthropic_client", None)

    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    resp = await client.post("/api/receipts/ocr/", files=files)
    assert resp.status_code == 200
    assert resp.json()["confidence"] == "low"


# ─── ЧП E: new-standard OCR fields + backward-compat aliases ──────────
async def test_ocr_aliases_backward_compat(client, monkeypatch):
    """New rich response from Claude → the old aliases the frontend reads exist."""
    payload = {
        "org_legal": 'ООО "Денежные энергии"',
        "org_brand": "Aster",
        "org_inn": "7707083893",
        "address": "СПб, Невский 1",
        "datetime": "2026-05-21T12:17:00",
        "amount": 6660.0,
        "currency": "RUB",
        "operation_type": "purchase",
        "payment_form": "card",
        "payment_detail": "Корпоративная 3950",
        "card_last4": "3950",
        "tax_system": "usn_income",
        "vat_20": 1110.0,
        "vat_10": None,
        "vat_0": 5550.0,
        "cashier": "Дробушков Никита",
        "items": [
            {
                "position": 1,
                "name": "Шакшука",
                "quantity": 1.0,
                "price": 750.0,
                "sum": 750.0,
                "vat_rate": "20",
            }
        ],
        "confidence": "high",
    }
    import json as _json

    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    body = (await client.post("/api/receipts/ocr/", files=files)).json()

    # rich fields preserved
    assert body["org_brand"] == "Aster"
    assert body["tax_system"] == "usn_income"
    assert body["vat_0"] == 5550.0
    # backward-compat aliases the current frontend (handleOcrFile) reads
    assert body["org"] == "Aster"  # org_brand or org_legal
    assert body["amount"] == 6660.0
    assert body["date"] == "2026-05-21"  # from datetime
    assert body["time"] == "12:17:00"
    assert body["payment_type"] == "card"  # from payment_form
    assert body["inn"] == "7707083893"  # alias of org_inn
    assert body["category"]  # auto-categorized from org
    assert body["nds"] == 1110.0  # vat_20 + vat_10(None)
    assert body["items"][0]["total"] == 750.0  # sum aliased to total


async def test_ocr_invalid_inn_returns_null(client, monkeypatch):
    """An OCR-misread INN with a bad checksum is dropped + a warning is added."""
    payload = {
        "org_brand": "Лавка",
        "amount": 100.0,
        "org_inn": "1234567890",
        "confidence": "high",
    }
    import json as _json

    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    body = (await client.post("/api/receipts/ocr/", files=files)).json()
    assert body["org_inn"] is None
    assert body["inn"] is None
    assert any("ИНН" in w for w in body["warnings"])


async def test_ocr_datetime_formats(client, monkeypatch):
    """Assorted human datetime formats normalize to ISO; junk → None."""
    import json as _json

    cases = {
        "2026-05-21T12:17:00": "2026-05-21T12:17:00",
        "21.05.2026 12:17": "2026-05-21T12:17:00",
        "21.05.2026": "2026-05-21T00:00:00",
        "2026-05-21": "2026-05-21T00:00:00",
        "не дата": None,
    }
    for raw, expected in cases.items():
        payload = {
            "org_brand": "X",
            "amount": 1.0,
            "datetime": raw,
            "confidence": "high",
        }
        _install_fake(monkeypatch, lambda kw, p=payload: _Response(_json.dumps(p)))
        files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
        body = (await client.post("/api/receipts/ocr/", files=files)).json()
        assert body["datetime"] == expected, f"{raw!r} → {body['datetime']!r}"


async def test_ocr_partial_response_fallback(client, monkeypatch):
    """No org / no amount → aliases are None, so the frontend shows 'partial'."""
    payload = {"address": "СПб", "confidence": "low"}  # neither org nor amount
    import json as _json

    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    body = (await client.post("/api/receipts/ocr/", files=files)).json()
    assert body["org"] is None  # frontend: !d.org → "partial"
    assert body["amount"] is None


async def test_ocr_no_fiscal_fields_requested(client, monkeypatch):
    """Промпт не просит РЕКВИЗИТЫ — но просит признак документа (№25, Б).

    ⚠️ УТВЕРЖДЕНИЕ ИЗМЕНИЛОСЬ 04.09.2026, И ЭТО РЕШЕНИЕ ВЛАДЕЛЬЦА, А НЕ
    ПОСЛАБЛЕНИЕ. Было: «не проси ничего фискального». Стало: ФН, ЗН и РН
    по-прежнему нельзя — опечатка в них портит реквизит; а ФД и ФПД просим
    ОТДЕЛЬНЫМИ ключами `ocr_fd`/`ocr_fpd`, которые в карточку чека не
    попадают и реквизитами не считаются. Ошибка в цифре тогда даёт
    «не дубль», а не ложь в документе.

    Сторож остался сторожем: он всё так же требует, чтобы модель НЕ
    заполняла настоящие `fd_num`/`fpd` — иначе распознанное поехало бы
    в реквизиты той же дорогой, что и раньше.
    """
    captured = {}

    def capture(kw):
        captured["prompt"] = kw["messages"][0]["content"][1]["text"]
        return _Response('{"org_brand": "X", "amount": 1, "confidence": "high"}')

    _install_fake(monkeypatch, capture)
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    await client.post("/api/receipts/ocr/", files=files)
    prompt = captured["prompt"]
    for key in ("kkt_fn", "kkt_rn", "kkt_serial", "fiscalDriveNumber"):
        assert key not in prompt, f"{key} — реквизит, распознаванию не отдаём"
    for реквизит in ('"fd_num"', '"fpd"'):
        assert реквизит not in prompt, (
            f"{реквизит} — колонка реквизита; модель заполнять её не должна"
        )
    for признак in ('"ocr_fd"', '"ocr_fpd"'):
        assert признак in prompt, f"{признак} нужен для поиска повторного фото"


# ─── строка 24: неправдоподобная дата попадает в warnings ручки OCR ───
async def test_ocr_warns_about_implausible_date(client, monkeypatch):
    """Ровно случай 12.08.2026: модель вернула 2024 год на сегодняшний чек.

    Проверяем, что предупреждение доезжает ДО КЛИЕНТА, а не остаётся
    в чистой функции: честный признак, которого никто не видит, — то же
    самое, что его отсутствие.
    """
    import json as _json

    payload = {
        "org_brand": "Ресторан",
        "amount": 3500,
        "datetime": "2024-12-26T15:15:00",
        "confidence": "high",
    }
    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    body = (await client.post("/api/receipts/ocr/", files=files)).json()

    assert body["date"] == "2024-12-26", "дата модели сохраняется как есть"
    assert any("26.12.2024" in w for w in body["warnings"]), (
        "неправдоподобная дата обязана попасть в warnings ответа"
    )


async def test_ocr_does_not_warn_about_normal_date(client, monkeypatch):
    # Ложная тревога учит не смотреть на предупреждения — проверяем обе стороны.
    import json as _json

    payload = {
        "org_brand": "Ресторан",
        "amount": 3500,
        "datetime": date.today().strftime("%Y-%m-%dT12:00:00"),
        "confidence": "high",
    }
    _install_fake(monkeypatch, lambda kw: _Response(_json.dumps(payload)))
    files = {"file": ("r.png", io.BytesIO(_PNG_1x1), "image/png")}
    body = (await client.post("/api/receipts/ocr/", files=files)).json()
    assert body["warnings"] == []
