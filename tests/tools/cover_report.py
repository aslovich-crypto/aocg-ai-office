"""Сводка замера покрытия ручек: все маршруты против реально дёрнутых.

Читает JSON, который оставил плагин `cover_routes.py` (см. его шапку),
и печатает по роутерам. Запуск:

    JWT_SECRET_KEY=любой ./venv/bin/python tests/tools/cover_report.py /tmp/cover.json

Переменная нужна потому, что импорт приложения без неё намеренно падает
(отказ подписывать токены небезопасным дефолтом) — здесь она фиктивная
и ни на что не влияет: ни один запрос не выполняется.
"""

import json
import os
import sys

sys.path.insert(0, os.getcwd())

import app.main as m  # noqa: E402

УСПЕХ = {200, 201, 204}


def main(путь):
    seen = json.load(open(путь))
    строки = []
    for r in m.app.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None) or set()
        if not path or not path.startswith("/api"):
            continue
        for meth in sorted(methods):
            if meth in ("HEAD", "OPTIONS"):
                continue
            коды = seen.get(meth + " " + path)
            if коды is None:
                состояние = "НЕ ВЫЗЫВАЛСЯ"
            elif any(c in УСПЕХ for c in коды):
                состояние = "успех " + str(коды)
            else:
                состояние = "ТОЛЬКО ОШИБКИ " + str(коды)
            модуль = getattr(getattr(r, "endpoint", None), "__module__", "?").split(
                "."
            )[-1]
            строки.append((модуль, состояние, meth + " " + path))

    строки.sort()
    текущий = None
    итог = {"НЕ ВЫЗЫВАЛСЯ": 0, "ТОЛЬКО ОШИБКИ": 0, "успех": 0}
    for модуль, состояние, ключ in строки:
        if модуль != текущий:
            текущий = модуль
            print("\n### " + модуль + ".py")
        print(f"  {состояние:26} {ключ}")
        итог[
            состояние.split()[0]
            if состояние.startswith("успех")
            else " ".join(состояние.split()[:2])
        ] += 1
    print("\nИТОГО:", итог, "из", len(строки))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/cover_routes.json")
