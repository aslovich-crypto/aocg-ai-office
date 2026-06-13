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

    app = Starlette(routes=[Route("/ping", ok), Route("/api/auth/login", ok)])
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
