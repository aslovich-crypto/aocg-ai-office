"""Какие ручки открыты анониму — и почему именно эти (задача S-33).

ПОВОД. 07.08.2026 я заявил, что `GET /api/services/` была ЕДИНСТВЕННОЙ
ручкой без `Depends(get_current_user)`. Это оказалось неверно: проверка
по КОДУ (`ast`, а не глазами) нашла двенадцать открытых ручек, и четыре
из них открытыми быть не должны — OCR, прокси ФНС и обе ручки согласия.

ЧТО ЗАКРЫТО И ЧЕМ ЭТО ГРОЗИЛО:
  • `POST /api/receipts/ocr/` — аноним жёг платный ключ Anthropic
    и отправлял произвольные изображения ЗА РУБЕЖ. По 152-ФЗ
    трансграничная передача возможна при согласии субъекта, а у анонима
    согласия нет по определению;
  • `POST /api/fns/check` — прокси к проверкачека.com НАШИМ платным
    токеном: чужой человек расходовал нашу квоту;
  • `POST /api/consent/` — запись в журнал согласий 152-ФЗ. Открытая
    ручка позволяла НАПИСАТЬ согласие за любого user_id, то есть
    подделать юридическое доказательство;
  • `GET /api/consent/{user_id}` — и прочитать чужую запись согласия.

ЧТО ОСТАЁТСЯ ОТКРЫТЫМ НАМЕРЕННО: восемь ручек `auth.py` — вход,
регистрация, подтверждение почты, обновление и выход, проверка
приглашения, регистрация по ссылке и справочник ЕГРЮЛ. Их дёргает
человек, которого в системе ещё нет; закрыть их значит закрыть вход.
Этот список проверяется тестом ниже — новая открытая ручка не появится
незаметно.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

# Ровно те ручки, которым положено работать без токена. Список ЯВНЫЙ:
# сверяется с кодом, поэтому и новая открытая ручка, и случайно закрытая
# ручка входа обе дадут красный тест.
ОТКРЫТЫ_НАМЕРЕННО = {
    "POST /api/auth/register",
    "GET /api/auth/verify-email",
    "POST /api/auth/login",
    "POST /api/auth/refresh",
    "POST /api/auth/logout",
    "GET /api/invite/validate/{token}",
    "POST /api/auth/register-by-invite",
    "GET /api/egrul/{inn}",
}


def _открытые_ручки():
    """(метод, путь) ручек без зависимости get_current_user — по коду."""
    import ast
    import pathlib

    корень = pathlib.Path(__file__).resolve().parents[1] / "app" / "routers"
    открытые, всего = set(), 0
    for файл in sorted(корень.glob("*.py")):
        tree = ast.parse(файл.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            точка = None
            for d in fn.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
                    метод = d.func.attr.upper()
                    if метод in ("GET", "POST", "PATCH", "PUT", "DELETE"):
                        путь = (
                            d.args[0].value
                            if d.args and isinstance(d.args[0], ast.Constant)
                            else ""
                        )
                        префикс = "/api"
                        for узел in ast.walk(tree):
                            if (
                                isinstance(узел, ast.Call)
                                and getattr(узел.func, "id", "") == "APIRouter"
                            ):
                                for kw in узел.keywords:
                                    if kw.arg == "prefix" and isinstance(
                                        kw.value, ast.Constant
                                    ):
                                        префикс = kw.value.value
                        точка = f"{метод} {префикс}{путь}".rstrip("/")
            if not точка:
                continue
            всего += 1
            есть = any(
                isinstance(d, ast.Call)
                and getattr(d.func, "id", "") == "Depends"
                and d.args
                and getattr(d.args[0], "id", "") == "get_current_user"
                for d in list(fn.args.defaults) + list(fn.args.kw_defaults)
            )
            if not есть:
                открытые.add(точка)
    return открытые, всего


def test_open_endpoints_are_exactly_the_login_flow():
    """Источник охвата + сам список: ручек разобрано много, открыты — только эти."""
    открытые, всего = _открытые_ручки()
    assert всего >= 45, f"разобрано ручек {всего} — сломан разбор, а не «всё закрыто»"
    лишние = открытые - {p.rstrip("/") for p in ОТКРЫТЫ_НАМЕРЕННО}
    пропали = {p.rstrip("/") for p in ОТКРЫТЫ_НАМЕРЕННО} - открытые
    assert not лишние, (
        "Появилась ручка БЕЗ авторизации. Если так и задумано — добавьте её "
        f"в ОТКРЫТЫ_НАМЕРЕННО с обоснованием: {sorted(лишние)}"
    )
    assert not пропали, (
        "Ручка входа закрылась авторизацией — в неё теперь не попасть тому, "
        f"кого ещё нет в системе: {sorted(пропали)}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "метод,путь,тело",
    [
        ("post", "/api/receipts/ocr/", None),
        ("post", "/api/fns/check", {"qr_raw": "t=1&s=1"}),
        ("post", "/api/consent/", {"user_id": "local_user"}),
        ("get", "/api/consent/local_user", None),
    ],
)
async def test_gated_endpoints_reject_anonymous(db, метод, путь, тело):
    """Аноним получает отказ — и побочного эффекта не происходит."""
    app.dependency_overrides.clear()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await getattr(c, метод)(путь, **({"json": тело} if тело else {}))
    assert r.status_code in (401, 403), f"{путь} → {r.status_code}: {r.text[:120]}"
    assert db.consents == [], "анонимная запись согласия не должна была появиться"


@pytest.mark.asyncio
async def test_consent_still_works_for_authorized(client, db):
    """Положительная половина: свой пишет и читает согласие как раньше."""
    # Строка 9: субъект берётся из токена (в фикстуре client это id=1),
    # поэтому и читаем по нему, а не по легаси-маркеру.
    записано = await client.post("/api/consent/", json={})
    assert записано.status_code == 200, записано.text
    прочитано = await client.get("/api/consent/1")
    assert прочитано.status_code == 200, прочитано.text
    assert прочитано.json()["policy_version"] == записано.json()["policy_version"]
