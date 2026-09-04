# -*- coding: utf-8 -*-
"""Потолки ослабления: панель может ужесточить защиту, но не ослабить (S-49).

⚠️ ЗАЧЕМ, ЗАМЕРОМ. 27.08.2026 сверка панели Timeweb с кодом нашла
`SECURITY_AUTH_RATE_LIMIT` = 50 при умолчании кода 5. 04.09.2026 замер
ПОВЕДЕНИЕМ прода подтвердил: семь попыток входа подряд — семь раз 401
и ни одного отказа по частоте. Защита от перебора жила в панели, а 502
зелёных теста мерили умолчание кода: прибор смотрел не туда, где стояло
значение.

⚠️ ЭТИ ТЕСТЫ РАБОТАЮТ С НАСТОЯЩИМИ ПОТОЛКАМИ. Общий `tests/conftest.py`
поднимает их до 100000, иначе сотни запросов к `/api/auth/*` с одного
адреса упирались бы в предел. Здесь значения возвращаются на место
фикстурой — иначе тест проверял бы подменённую константу, то есть
собственную выдумку (класс «двойник вернее кода», T136).
"""

import importlib

import pytest

import aocg_security.middleware as мидлварь


@pytest.fixture
def настоящие_потолки():
    """Возвращает КОДОВЫЕ значения потолков на время одного теста."""
    свежий = importlib.import_module("aocg_security.middleware")
    было = (свежий.ПОТОЛОК_СТРОГОГО_ЛИМИТА, свежий.ПОТОЛОК_ПОРОГА_БАНА)
    # Значения берутся не из головы: они объявлены в модуле и здесь лишь
    # восстанавливаются после подмены в conftest.
    свежий.ПОТОЛОК_СТРОГОГО_ЛИМИТА = 20
    свежий.ПОТОЛОК_ПОРОГА_БАНА = 20
    yield свежий
    свежий.ПОТОЛОК_СТРОГОГО_ЛИМИТА, свежий.ПОТОЛОК_ПОРОГА_БАНА = было


def _мидлварь_с_окружением(monkeypatch, **переменные):
    for имя, значение in переменные.items():
        monkeypatch.setenv(имя, значение)
    return мидлварь.AOCGSecurityMiddleware(app=lambda *a, **k: None)


def test_панель_не_может_ослабить_строгий_лимит(monkeypatch, настоящие_потолки, caplog):
    """Ровно тот случай, что стоял на проде: в панели 50 при потолке 20."""
    с = _мидлварь_с_окружением(monkeypatch, SECURITY_AUTH_RATE_LIMIT="50")
    assert с.auth_rate_limit == 20, (
        "значение из панели применилось как есть — потолок не работает"
    )
    assert any("SECURITY_AUTH_RATE_LIMIT" in з.message for з in caplog.records), (
        "расхождение панели с кодом обязано оставить след в журнале: "
        "иначе следующий человек будет искать причину так же долго"
    )


def test_панель_МОЖЕТ_ужесточить(monkeypatch, настоящие_потолки):
    """Обратная сторона: потолок ограничивает ослабление, а не настройку.

    Запрет «менять вообще» выглядел бы рабочим на тесте выше и отнял бы
    у владельца единственный быстрый способ закрутить гайки при атаке.
    """
    с = _мидлварь_с_окружением(monkeypatch, SECURITY_AUTH_RATE_LIMIT="3")
    assert с.auth_rate_limit == 3


def test_порог_бана_тоже_под_потолком(monkeypatch, настоящие_потолки):
    """Большой порог обесценивает лимиты: превышай сколько угодно."""
    с = _мидлварь_с_окружением(monkeypatch, SECURITY_AUTO_BAN_THRESHOLD="1000")
    assert с.ban_threshold == 20


def test_https_выключается_только_явным_словом(monkeypatch, настоящие_потолки):
    """⚠️ ОПЕЧАТКА НЕ ДОЛЖНА ОТКРЫВАТЬ КАНАЛ.

    Прежняя проверка считала истиной перечисленные слова, а всё остальное —
    ложью: «tru» вместо «true» читалось как «выключить принуждение HTTPS».
    Промах пальцем в панели открывал канал, и ни ошибки, ни строки в журнале.
    """
    for опечатка in ("tru", "True!", "выключено", ""):
        с = _мидлварь_с_окружением(monkeypatch, SECURITY_ENFORCE_HTTPS=опечатка)
        assert с.enforce_https is True, f"«{опечатка}» сняло защиту"


def test_https_всё_же_можно_выключить_явно(monkeypatch, настоящие_потолки):
    """Локальной разработке выключатель нужен — он есть, но только явный."""
    for слово in ("false", "0", "no", "off", "FALSE", " false "):
        с = _мидлварь_с_окружением(monkeypatch, SECURITY_ENFORCE_HTTPS=слово)
        assert с.enforce_https is False, f"«{слово}» обязано выключать"


def test_аргумент_конструктора_потолком_не_режется(настоящие_потолки):
    """Явный аргумент — путь ТЕСТА, ему нужны любые значения.

    Панель и аргумент — разные источники: у первого ошибка не оставляет
    следа ни в git, ни в ревью, у второго она видна в коде.
    """
    с = мидлварь.AOCGSecurityMiddleware(
        app=lambda *a, **k: None, auth_rate_limit=100000
    )
    assert с.auth_rate_limit == 100000


def test_ручка_готовности_мимо_ограничений():
    """`/health/db` обязана быть исключена, как и `/health` (S-06 шаг 3).

    Иначе повторилась бы та же поломка: принуждение HTTPS отдаёт 403
    проверке по петле, площадка считает приложение больным и убивает
    контейнер по кругу.
    """
    assert "/health/db" in мидлварь.AOCGSecurityMiddleware.LIVENESS_EXEMPT
    assert "/health" in мидлварь.AOCGSecurityMiddleware.LIVENESS_EXEMPT


# ───────────────────── потолки срока жизни токенов ──────────────────────────


def test_срок_жизни_токена_не_длиннее_потолка(monkeypatch):
    """Долгий срок — окно для украденного токена, а не удобство.

    Перечитываем модуль: потолки применяются НА ИМПОРТЕ, как и на старте
    контейнера, — проверяем ровно тот путь, который отработает в проде.
    """
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "100000")
    monkeypatch.setenv("REFRESH_TOKEN_EXPIRE_DAYS", "3650")
    свежий = importlib.reload(importlib.import_module("app.auth"))
    try:
        assert свежий.ACCESS_TOKEN_EXPIRE_MINUTES == 240
        assert свежий.REFRESH_TOKEN_EXPIRE_DAYS == 90
    finally:
        monkeypatch.undo()
        importlib.reload(свежий)


def test_короче_потолка_панель_ставить_МОЖЕТ(monkeypatch):
    """Обратная сторона: ужесточать не запрещено."""
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
    свежий = importlib.reload(importlib.import_module("app.auth"))
    try:
        assert свежий.ACCESS_TOKEN_EXPIRE_MINUTES == 15
    finally:
        monkeypatch.undo()
        importlib.reload(свежий)


def test_алгоритм_подписи_только_из_белого_списка(monkeypatch):
    """⚠️ `none` отключает подпись, `RS256` открывает подмену ключа.

    Проверяется именно БЕЛЫЙ список: запрет одного «none» оставил бы
    открытым семейство асимметричных алгоритмов, где наш секрет играл бы
    роль публичного ключа.
    """
    for опасный in ("none", "None", "RS256", "мусор"):
        monkeypatch.setenv("JWT_ALGORITHM", опасный)
        свежий = importlib.reload(importlib.import_module("app.auth"))
        try:
            assert свежий.JWT_ALGORITHM == "HS256", f"«{опасный}» прошёл"
        finally:
            monkeypatch.undo()
            importlib.reload(свежий)


# ─────────── сверка окружения с описью на старте (S-49, сторож ②) ───────────


def test_сверка_молчит_когда_всё_сошлось(caplog):
    """Один WARNING на старт — чтобы «прибор работал» отличалось от «молчал»."""
    from app import env_check

    хорошее = {
        "CORS_ORIGINS": "https://app.aocgai.ru",
        "POSTBOX_SMTP_HOST": "h",
        "POSTBOX_SMTP_USER": "u",
        "POSTBOX_SMTP_PASSWORD": "p",
        "POSTBOX_FROM": "f",
        "MAX_RELAY_TOKEN": "t",
        "S3_BUCKET": "b",
        "S3_ENDPOINT": "e",
        "SECURITY_AUTH_RATE_LIMIT": "10",
    }
    assert env_check.сверить(хорошее) == []
    with caplog.at_level("WARNING"):
        assert env_check.сверить_и_сказать(хорошее) == 0
    assert any("сошлись с описью" in з.message for з in caplog.records)


def test_сверка_называет_каждое_расхождение(caplog):
    """⚠️ Ровно то, чего не хватало: панель против описи, видно в журнале.

    Проверяется и то, что расхождение НАЗВАНО ПОИМЕННО: строка «расхождений
    3» без имён отправила бы владельца искать вручную по всей панели.
    """
    from app import env_check

    плохое = {
        "SECURITY_AUTH_RATE_LIMIT": "50",  # слабее потолка
        "SECURITY_ENFORCE_HTTPS": "false",  # защита выключена
        "BOOTSTRAP_ADMIN_EMAIL": "chuzhoy@example.com",  # обязана быть пустой
        "CORS_ORIGINS": "https://app.aocgai.ru",
        "POSTBOX_SMTP_HOST": "h",
        "POSTBOX_SMTP_USER": "u",
        "POSTBOX_SMTP_PASSWORD": "p",
        "POSTBOX_FROM": "f",
        "MAX_RELAY_TOKEN": "t",
        "S3_BUCKET": "b",
        "S3_ENDPOINT": "e",
    }
    имена = {имя for имя, _видно, _угроза in env_check.сверить(плохое)}
    assert имена == {
        "SECURITY_AUTH_RATE_LIMIT",
        "SECURITY_ENFORCE_HTTPS",
        "BOOTSTRAP_ADMIN_EMAIL",
    }
    with caplog.at_level("WARNING"):
        assert env_check.сверить_и_сказать(плохое) == 3
    # getMessage() подставляет аргументы; `.message % .args` их удваивает.
    сказано = " ".join(з.getMessage() for з in caplog.records)
    for имя in имена:
        assert имя in сказано, f"{имя} не назван в журнале"


def test_секрет_в_журнал_не_попадает():
    """Журнал деплоя читают в панели и копируют в переписку."""
    from app import env_check

    видно = env_check.показать("MAX_RELAY_TOKEN", "очень-секретное-значение")
    assert "очень-секретное" not in видно
    assert "задан" in видно and "24" in видно


def test_отсутствие_переменной_с_безопасным_умолчанием_НЕ_расхождение():
    """⚠️ Сверка владельца 04.09.2026: половины переменных в панели нет,

    и это НОРМА — умолчание кода безопасно. Прибор, кричащий на каждое
    отсутствие, научил бы не смотреть на него вовсе.
    """
    from app import env_check

    основа = {
        "CORS_ORIGINS": "https://app.aocgai.ru",
        "POSTBOX_SMTP_HOST": "h",
        "POSTBOX_SMTP_USER": "u",
        "POSTBOX_SMTP_PASSWORD": "p",
        "POSTBOX_FROM": "f",
        "MAX_RELAY_TOKEN": "t",
        "S3_BUCKET": "b",
        "S3_ENDPOINT": "e",
    }
    # Ни JWT_ALGORITHM, ни сроков, ни SECURITY_* — как в панели на 04.09.2026.
    assert env_check.сверить(основа) == []


# ─────────────── схема API закрыта на проде (S-64) ───────────────────────────


def _поднять_приложение(monkeypatch, **переменные):
    """Пересобирает app с заданным окружением: решение принимается на импорте."""
    import importlib

    for имя, значение in переменные.items():
        monkeypatch.setenv(имя, значение)
    return importlib.reload(importlib.import_module("app.main")).app


def test_схема_api_закрыта_на_проде(monkeypatch):
    """⚠️ Оглавление всех ручек, выданное сканеру бесплатно (S-64).

    Замер 04.09.2026: /docs, /redoc и /openapi.json отвечали 200 кому
    угодно, включая формы `/internal/max/*` — тех самых, чья защита
    держится на одном статическом токене.
    """
    приложение = _поднять_приложение(monkeypatch, ENVIRONMENT="production")
    try:
        assert приложение.docs_url is None
        assert приложение.redoc_url is None
        assert приложение.openapi_url is None
    finally:
        monkeypatch.undo()
        import importlib

        importlib.reload(importlib.import_module("app.main"))


def test_вне_прода_схема_открыта(monkeypatch):
    """Обратная сторона: отнимать схему у разработки нельзя."""
    приложение = _поднять_приложение(monkeypatch, ENVIRONMENT="local")
    try:
        assert приложение.docs_url == "/docs"
        assert приложение.openapi_url == "/openapi.json"
    finally:
        monkeypatch.undo()
        import importlib

        importlib.reload(importlib.import_module("app.main"))


def test_незаданная_переменная_не_открывает_схему(monkeypatch):
    """⚠️ ПРАВИЛО S-49 В ДЕЙСТВИИ: умолчание не ослабляет защиту.

    ENVIRONMENT не задана — считаем, что это прод, и закрываем. Обратное
    умолчание («открыто, пока не запретили») и есть тот класс, ради
    которого заведена S-49.
    """
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("API_DOCS_PUBLIC", raising=False)
    import importlib

    приложение = importlib.reload(importlib.import_module("app.main")).app
    try:
        assert приложение.docs_url is None
    finally:
        monkeypatch.undo()
        importlib.reload(importlib.import_module("app.main"))
