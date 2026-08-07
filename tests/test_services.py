"""Статус интеграций: только для своих (задача S-32).

Ручка `GET /api/services/` работала без `Depends(get_current_user)` —
нашлось замером покрытия (S-31): она не вызывалась в тестах ни разу,
поэтому и отсутствие зависимости никто не заметил.

ПОПРАВКА 07.08.2026: в отчёте я назвал её ЕДИНСТВЕННОЙ такой ручкой —
это было неверно и не проверено. Проверка по КОДУ нашла двенадцать
открытых ручек, четыре из них закрыты в S-33 (OCR, прокси ФНС, обе
ручки согласия); список открытых теперь держит
`tests/test_public_endpoints.py`.

ПД ручка не отдаёт, но показывает состав интеграций и то, настроен ли ключ
OCR, то есть внутреннее состояние конфигурации.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_anonymous_gets_nothing(db):
    """Без токена — 401/403, и списка интеграций в ответе нет.

    Клиент поднимается ОТДЕЛЬНО, без подмены get_current_user: остальные
    фикстуры её подменяют, и «аноним» под ними был бы не анонимом.
    """
    app.dependency_overrides.clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/services/")
    assert r.status_code in (401, 403), r.text
    assert "anthropic" not in r.text, "состав интеграций не должен светиться анониму"


@pytest.mark.asyncio
async def test_employee_still_sees_services(client_employee):
    """Положительный случай: своим вкладка «Сервисы» работает как раньше."""
    r = await client_employee.get("/api/services/")
    assert r.status_code == 200, r.text
    ключи = {s["key"] for s in r.json()}
    assert {"fns", "alfabank", "anthropic"} <= ключи
