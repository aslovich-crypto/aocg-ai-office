"""Backend-покрытие vendored AOCGSecurityMiddleware.

Один тест бьёт по РЕАЛЬНОМУ app (через client-фикстуру) — подтверждает, что
middleware реально подключён в app.main и навешивает security-заголовки.
Остальные строят отдельный app с явными низкими лимитами (конструктор имеет
приоритет над env, поэтому тест-дефолты conftest тут не мешают) — проверяют
429 на /api/auth/* и что обычный путь не задет строгим auth-лимитом.
"""

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from aocg_security.middleware import AOCGSecurityMiddleware


async def test_real_app_emits_security_headers(client):
    # Middleware подключён в app.main → security-заголовки есть в ответе.
    resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def _standalone(**cfg):
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route("/ping", ok),
            Route("/api/auth/login", ok),
            Route("/api/auth/refresh", ok),
            Route("/api/auth/me", ok),
        ]
    )
    app.add_middleware(AOCGSecurityMiddleware, **cfg)
    return TestClient(app)


def test_mw_normal_request_passes():
    c = _standalone(enforce_https=False, rate_limit=100, auth_rate_limit=100)
    assert c.get("/ping").status_code == 200


def test_mw_auth_path_rate_limit_429():
    c = _standalone(enforce_https=False, rate_limit=1000, auth_rate_limit=2)
    codes = [c.get("/api/auth/login").status_code for _ in range(3)]
    assert codes == [200, 200, 429]  # строгий auth-лимит 2 → 3-й заблокирован
    general = [c.get("/ping").status_code for _ in range(3)]
    assert general == [200, 200, 200]  # обычный путь не задет (лимит 1000)


def test_mw_refresh_exempt_from_strict_auth_limit():
    # refresh — служебный: под ОБЩИМ лимитом (1000), не строгим auth (2).
    c = _standalone(enforce_https=False, rate_limit=1000, auth_rate_limit=2)
    assert [c.get("/api/auth/refresh").status_code for _ in range(5)] == [200] * 5
    # login под тем же строгим лимитом — 429 на 3-м (регресс-страховка).
    assert [c.get("/api/auth/login").status_code for _ in range(3)] == [200, 200, 429]


def test_mw_me_exempt_from_strict_auth_limit():
    c = _standalone(enforce_https=False, rate_limit=1000, auth_rate_limit=2)
    assert [c.get("/api/auth/me").status_code for _ in range(5)] == [200] * 5


def test_is_strict_auth_classification():
    mw = AOCGSecurityMiddleware(None, enforce_https=False)
    assert mw._is_strict_auth("/api/auth/login") is True
    assert mw._is_strict_auth("/api/auth/register") is True
    assert mw._is_strict_auth("/api/auth/logout") is True  # остаётся строгим
    assert mw._is_strict_auth("/api/auth/refresh") is False
    assert mw._is_strict_auth("/api/auth/me") is False
    assert mw._is_strict_auth("/api/receipts/") is False


# ────────────────────────────────────────────────────────────────────────────
# S-06 шаг 3: проверка состояния площадки идёт по петле и всегда по http.
# Пока /health не был исключён, принуждение HTTPS отдавало ей 403, Timeweb
# App Platform считал приложение больным и убивал контейнер по кругу.
# Три теста: исключение работает · исключение НЕ шире, чем надо · монитор
# не выжигает общий лимит (иначе та же смерть, но через 429 и автобан).
# ────────────────────────────────────────────────────────────────────────────


def _standalone_health(**cfg):
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/health", ok), Route("/ping", ok)])
    app.add_middleware(AOCGSecurityMiddleware, **cfg)
    return TestClient(app)


def test_health_passes_over_plain_http_when_https_enforced():
    # Как ходит монитор Timeweb: http, без x-forwarded-proto.
    c = _standalone_health(enforce_https=True, rate_limit=100, auth_rate_limit=100)
    resp = c.get("http://testserver/health")
    assert resp.status_code == 200, "проверка состояния должна проходить по http"
    # Заголовки безопасности при этом НЕ отваливаются.
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_enforce_https_still_blocks_other_paths():
    # Вторая половина: исключение не должно снять принуждение со всего app.
    c = _standalone_health(enforce_https=True, rate_limit=100, auth_rate_limit=100)
    resp = c.get("http://testserver/ping")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "HTTPS required"


def test_health_does_not_consume_general_rate_limit():
    # Монитор ходит с одного адреса чаще, чем лимит: он не должен ни сам
    # получить 429, ни съесть бюджет обычных запросов с того же адреса.
    c = _standalone_health(enforce_https=False, rate_limit=3, auth_rate_limit=3)
    for _ in range(10):
        assert c.get("/health").status_code == 200
    assert c.get("/ping").status_code == 200, "монитор выжег общий лимит"
