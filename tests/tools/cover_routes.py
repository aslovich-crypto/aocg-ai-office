"""Замер покрытия РУЧЕК: какие маршруты тесты дёргают и с какими кодами.

ЗАЧЕМ. Счётчик «292 теста прошли» ничего не говорит о том, что осталось
непройденным. 07.08.2026 выяснилось, что мутации `users` не покрывались
вовсе — в FakePool не было даже таблицы, — и дыра S-29 жила ровно там.
Вопрос «где ещё так же» решается не чтением тестов, а замером.

ПОЧЕМУ НЕ ГРЕПОМ ПО ТЕСТАМ. Первый заход считал вызовы `client.post("/api/…")`
регуляркой и соврал дважды: пропустил многострочные вызовы (URL на следующей
строке) и записал в покрытые `/api/auth/login`, который на самом деле дёргается
у СИНТЕТИЧЕСКОГО приложения в тестах rate-limiter, а не у нашего роутера.
Здесь считает сам ASGI-слой, промахнуться нечем.

КАК УСТРОЕНО. Оборачиваем `route.app` у каждого маршрута FastAPI (Starlette
зовёт его из `Route.handle`) и запоминаем метод, путь и коды ответов.
Ничего в приложении не меняется, тесты не знают о плагине.

ЗАПУСК:
    PYTHONPATH=tests/tools COVER_OUT=/tmp/cover.json \\
        ./venv/bin/python -m pytest tests/ -q -p cover_routes
    ./venv/bin/python tests/tools/cover_report.py /tmp/cover.json

ЧИТАТЬ РЕЗУЛЬТАТ. «НЕ ВЫЗЫВАЛСЯ» — SQL ручки не выполнялся ни разу.
«ТОЛЬКО ОШИБКИ» — до тела дошли лишь отказы (403/404/422), то есть рабочая
ветка тоже не проверялась: так выглядит ручка, у которой протестирован
гейт и не протестировано то, что он охраняет.
"""

import json
import os

RESULT = os.environ.get("COVER_OUT", "/tmp/cover_routes.json")
seen = {}


def pytest_configure(config):
    import app.main as m

    for r in m.app.routes:
        path = getattr(r, "path", None)
        inner = getattr(r, "app", None)
        methods = getattr(r, "methods", None)
        if path is None or inner is None or not methods:
            continue

        def make(inner, path):
            async def wrapped(scope, receive, send):
                key = scope["method"] + " " + path

                async def send2(message):
                    if message.get("type") == "http.response.start":
                        seen.setdefault(key, set()).add(message["status"])
                    await send(message)

                return await inner(scope, receive, send2)

            return wrapped

        r.app = make(inner, path)


def pytest_sessionfinish(session, exitstatus):
    with open(RESULT, "w") as f:
        json.dump(
            {k: sorted(v) for k, v in seen.items()}, f, ensure_ascii=False, indent=1
        )
