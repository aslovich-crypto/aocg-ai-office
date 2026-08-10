#!/usr/bin/env python3
"""ГЕНЕРАТОР словаря категорий из ОДНОГО источника (T39, шаг ②).

ЗАЧЕМ. Один и тот же словарь записан руками в шести местах на трёх языках,
и совпадать они обязаны. Замер `tests/tools/contract_dict.py` (шаг ①) показал
это числом; здесь у словаря появляется ИСТОЧНИК, из которого копии
не переписываются, а порождаются.

ПОЧЕМУ ИСТОЧНИК — НЕЙТРАЛЬНЫЙ JSON, А НЕ PYTHON-ФАЙЛ. Потребителей у словаря
шесть, и ТРИ ИЗ НИХ НЕ JAVASCRIPT: SQL CHECK в `app/database.py`, правила
автокатегоризации `app/categorization.py`, промпт распознавания
`app/routers/ocr.py` (последний — про ставки НДС, тот самый NDS-OCR22).
При python-источнике каждый новый потребитель обязан уметь импортировать
python; при JSON — просто читает данные. Второе: файл `.py` ничем не мешает
вписать в него ВЫЧИСЛЕНИЕ, и тогда генерация молча разъедется, а JSON
исключает это устройством. Третье: дифф в ревью — чистые данные.
JSON, а не YAML: python и js читают его без зависимостей, а `check json`
уже стоит в pre-commit — сторож формата достался бесплатно.

ПОЧЕМУ КОПИИ ЛЕЖАТ В GIT, А НЕ СОБИРАЮТСЯ НА ДЕПЛОЕ. Railway выкатывает
python без шага сборки: несгенерированная копия означала бы упавший старт.
Копия в git имеет и второе свойство — изменение словаря видно ДИФФОМ В РЕВЬЮ.

ЧТО ГЕНЕРИРУЕТСЯ:
  • `app/dictionaries/__init__.py` — TAX_KINDS, DEFAULT_CATEGORIES (данные
    целиком: 9 видов, 11 групп, 48 статей);
  • `<фронт>/src/lib/dictionaries.js` — TAX_KINDS и GROUPS (имена групп).
    Статьи фронту НЕ нужны: каталог он берёт из API (`setCatalogMaps`).

ЧЕГО ГЕНЕРАТОР НЕ КАСАЕТСЯ, НАМЕРЕННО:
  • `GROUP_COLORS` в `src/lib/categories.js` — имена групп там словарь,
    а ЦВЕТА решение дизайн-линии. Перезаписывать файл значило бы затирать
    дизайн правкой словаря. Совпадение ключей проверяет сторож
    `scripts/check-dictionaries.mjs`, а не генератор.
  • SQL CHECK в `app/database.py` — отдельный заход: правка DDL внутри
    `init_db()`, ошибка роняет старт приложения. Пока за этим стыком следит
    слой ② замера контракта.

СВЕРКА (`--check`) ИДЁТ ПО ДАННЫМ, А НЕ ПО БАЙТАМ. Причина конкретная:
`pre-commit` прогоняет `ruff format` и переписывает сгенерированный python,
то есть побайтовое сравнение краснело бы после каждого коммита на пустом
месте. Сравниваются РАЗОБРАННЫЕ значения — то же решение, что в
`design/freshness.mjs` во фронте, где сверяются токены, а не байты.

ЗАПУСК:
    ./venv/bin/python tools/gen_dictionaries.py            # записать копии
    ./venv/bin/python tools/gen_dictionaries.py --check    # только сверить
    FRONT_SRC=/путь/к/src ./venv/bin/python tools/gen_dictionaries.py

КОДЫ ВОЗВРАТА:
    0 — копии отвечают источнику
    1 — копии разошлись с источником (при --check) либо записаны (без него)
    2 — фронт-репозиторий недоступен, ЕГО КОПИЯ НЕ ПРОВЕРЯЛАСЬ
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ИСТОЧНИК = os.path.join(КОРЕНЬ, "app", "dictionaries", "categories.json")
КОПИЯ_PY = os.path.join(КОРЕНЬ, "app", "dictionaries", "__init__.py")
ФРОНТ_SRC = os.environ.get(
    "FRONT_SRC",
    os.path.normpath(os.path.join(КОРЕНЬ, "..", "aocg-ai-office-web", "src")),
)
КОПИЯ_JS_ОТН = os.path.join("lib", "dictionaries.js")

ШТАМП_МЕТКА = "ШТАМП-ИСТОЧНИКА sha256:"


def прочитать_источник() -> tuple[dict, str]:
    сырьё = open(ИСТОЧНИК, "rb").read()
    return json.loads(сырьё.decode("utf-8")), hashlib.sha256(сырьё).hexdigest()


def шапка(штамп: str, чем_править: str) -> str:
    return (
        f"СГЕНЕРИРОВАНО tools/gen_dictionaries.py — РУКАМИ НЕ ПРАВИТЬ.\n"
        f"Источник: app/dictionaries/categories.json (репозиторий бэкенда).\n"
        f"{ШТАМП_МЕТКА} {штамп}\n"
        f"Правка руками будет потеряна при следующей генерации и КРАСНЕЕТ\n"
        f"у сторожа. Нужно изменить словарь — правится источник, потом {чем_править}."
    )


def строка_py(значение: str) -> str:
    """Литерал python в ДВОЙНЫХ кавычках — как их пишет `ruff format`.

    Не косметика: `repr()` даёт одинарные, pre-commit прогоняет `ruff format`
    и переписывает файл СРАЗУ ПОСЛЕ генерации. Копия остаётся верной (сверка
    идёт по данным, а не по байтам), но каждый прогон генератора давал бы дифф
    на пустом месте, а мутации теряли бы цель — что и случилось 10.08.2026
    с M1. Генерируемый файл обязан быть неподвижен под форматтером.
    """
    return json.dumps(значение, ensure_ascii=False)


def собрать_py(данные: dict, штамп: str) -> str:
    строки = [
        '"""' + шапка(штамп, "генератор") + '"""',
        "",
        "# Статья, которую бэкенд ставит чеку, когда правила не сработали.",
        "# Фронт по ней ветвит подсказку — имя обязано совпадать на обеих сторонах.",
        f"DEFAULT_FALLBACK = {строка_py(данные['default_fallback'])}",
        "",
        "# Вид расхода, подставляемый НОВОЙ статье справочника (бэкенд и фронт",
        "# обязаны брать один и тот же).",
        f"DEFAULT_TAX_KIND = {строка_py(данные['default_tax_kind'])}",
        "",
        "TAX_KINDS = (",
    ]
    строки += [f"    {строка_py(вид)}," for вид in данные["tax_kinds"]]
    строки += [")", "", "DEFAULT_CATEGORIES = ["]
    for группа in данные["groups"]:
        строки += ["    (", f"        {строка_py(группа['name'])},", "        ["]
        строки += [
            f"            ({строка_py(с['name'])}, {строка_py(с['tax_kind'])}),"
            for с in группа["articles"]
        ]
        строки += ["        ],", "    ),"]
    строки += ["]", ""]
    return "\n".join(строки)


def собрать_js(данные: dict, штамп: str) -> str:
    комментарий = "\n".join("// " + с for с in шапка(штамп, "генератор").split("\n"))
    виды = "\n".join(f'  "{в}",' for в in данные["tax_kinds"])
    группы = "\n".join(f'  "{г["name"]}",' for г in данные["groups"])
    return (
        f"{комментарий}\n\n"
        "// Статья, которую бэкенд ставит чеку, когда правила не сработали\n"
        "// (app/categorization.py DEFAULT_FALLBACK). Фронт по ней ветвит\n"
        "// подсказку — имя обязано совпадать на обеих сторонах.\n"
        f'export const DEFAULT_FALLBACK = "{данные["default_fallback"]}";\n\n'
        "// Вид расхода, подставляемый НОВОЙ статье справочника.\n"
        f'export const DEFAULT_TAX_KIND = "{данные["default_tax_kind"]}";\n\n'
        "// 9 видов расхода в налоговом учёте — зеркало CHECK-констрейнта\n"
        "// categories.tax_kind на бэке.\n"
        f"export const TAX_KINDS = [\n{виды}\n];\n\n"
        "// 11 групп справочника. Цвета групп живут в lib/categories.js\n"
        "// (GROUP_COLORS) — это решение дизайн-линии, оно НЕ генерируется;\n"
        "// сторож check-dictionaries.mjs следит, что ключи там совпадают\n"
        "// с этим списком.\n"
        f"export const GROUPS = [\n{группы}\n];\n"
    )


def штамп_из(текст: str) -> str | None:
    м = re.search(rf"{re.escape(ШТАМП_МЕТКА)}\s*([0-9a-f]{{64}})", текст)
    return м.group(1) if м else None


def данные_из_py(текст: str) -> dict | None:
    """Разобрать python-копию БЕЗ импорта: сверка не должна зависеть от того,
    импортируется ли пакет (а он мог быть сломан ровно тем, что мы ищем)."""
    пространство: dict = {}
    try:
        exec(compile(текст, "<копия>", "exec"), пространство)  # noqa: S102
    except Exception:  # noqa: BLE001
        return None
    if "TAX_KINDS" not in пространство or "DEFAULT_CATEGORIES" not in пространство:
        return None
    return {
        "default_fallback": пространство.get("DEFAULT_FALLBACK"),
        "default_tax_kind": пространство.get("DEFAULT_TAX_KIND"),
        "tax_kinds": list(пространство["TAX_KINDS"]),
        "groups": [
            {"name": г, "articles": [{"name": и, "tax_kind": т} for и, т in статьи]}
            for г, статьи in пространство["DEFAULT_CATEGORIES"]
        ],
    }


def данные_из_js(текст: str) -> dict | None:
    def массив(имя: str) -> list[str] | None:
        м = re.search(rf"export const {имя} = \[(.*?)\];", текст, re.S)
        return re.findall(r'"([^"]*)"', м.group(1)) if м else None

    виды, группы = массив("TAX_KINDS"), массив("GROUPS")
    ф = re.search(r'export const DEFAULT_FALLBACK = "([^"]*)";', текст)
    в = re.search(r'export const DEFAULT_TAX_KIND = "([^"]*)";', текст)
    if виды is None or группы is None or ф is None or в is None:
        return None
    return {
        "default_fallback": ф.group(1),
        "default_tax_kind": в.group(1),
        "tax_kinds": виды,
        "groups": группы,
    }


def сверить(имя: str, факт, ждём) -> bool:
    if факт == ждём:
        print(f"  ✓ {имя}: отвечает источнику")
        return True
    print(f"  ✗ {имя}: РАЗОШЛАСЬ С ИСТОЧНИКОМ")
    if isinstance(ждём, list) and isinstance(факт, list):
        print(f"      нет в копии: {[з for з in ждём if з not in факт]}")
        print(f"      лишнее в копии: {[з for з in факт if з not in ждём]}")
    return False


def main() -> int:
    проверка = "--check" in sys.argv
    данные, штамп = прочитать_источник()
    статей = sum(len(г["articles"]) for г in данные["groups"])
    print(
        f"ИСТОЧНИК: {len(данные['tax_kinds'])} видов · {len(данные['groups'])} групп · "
        f"{статей} статей · sha256 {штамп[:12]}…"
    )

    путь_js = os.path.join(ФРОНТ_SRC, КОПИЯ_JS_ОТН)
    фронт_доступен = os.path.isdir(ФРОНТ_SRC)

    if not проверка:
        open(КОПИЯ_PY, "w", encoding="utf-8").write(собрать_py(данные, штамп))
        # Форматтер — ПОСЛЕДНИЙ шаг генерации, а не чужая правка после неё.
        # Иначе pre-commit переписывал бы файл сразу после каждой генерации:
        # дифф на пустом месте, а мутации теряли бы цель (случилось с M1
        # 10.08.2026). Воспроизводить переносы строк логикой генератора хрупко —
        # проще отдать это тому, кто их и расставляет.
        norm = subprocess.run(
            [sys.executable, "-m", "ruff", "format", "-q", КОПИЯ_PY],
            capture_output=True,
            text=True,
        )
        if norm.returncode != 0:
            print(f"  ⚠ ruff format не отработал: {norm.stderr.strip()[:120]}")
        print(f"  записано: {os.path.relpath(КОПИЯ_PY, КОРЕНЬ)}")
        if фронт_доступен:
            os.makedirs(os.path.dirname(путь_js), exist_ok=True)
            open(путь_js, "w", encoding="utf-8").write(собрать_js(данные, штамп))
            print(f"  записано: {путь_js}")
        else:
            print("  ⚠ ФРОНТ НЕДОСТУПЕН — его копия НЕ ЗАПИСАНА, а не «не нужна»")
            return 2
        return 0

    сошлось = True
    py_текст = open(КОПИЯ_PY, encoding="utf-8").read()
    if штамп_из(py_текст) != штамп:
        print(
            f"  ✗ {os.path.relpath(КОПИЯ_PY, КОРЕНЬ)}: ШТАМП НЕ СОВПАЛ "
            f"(в копии {штамп_из(py_текст) or '—'}, у источника {штамп[:12]}…)"
        )
        сошлось = False
    py_данные = данные_из_py(py_текст)
    if py_данные is None:
        print(f"  ✗ {os.path.relpath(КОПИЯ_PY, КОРЕНЬ)}: копия не разбирается")
        сошлось = False
    else:
        сошлось &= сверить(
            "python-копия: фолбэк",
            py_данные["default_fallback"],
            данные["default_fallback"],
        )
        сошлось &= сверить(
            "python-копия: вид по умолчанию",
            py_данные["default_tax_kind"],
            данные["default_tax_kind"],
        )
        сошлось &= сверить(
            "python-копия: виды", py_данные["tax_kinds"], данные["tax_kinds"]
        )
        сошлось &= сверить(
            "python-копия: состав",
            py_данные["groups"],
            данные["groups"],
        )

    if not фронт_доступен:
        print("  ⚠ ФРОНТ-РЕПОЗИТОРИЙ НЕДОСТУПЕН: его копия НЕ ПРОВЕРЯЛАСЬ")
        print(f"     ждали каталог {ФРОНТ_SRC}")
        return 2 if сошлось else 1

    js_текст = open(путь_js, encoding="utf-8").read()
    if штамп_из(js_текст) != штамп:
        print(f"  ✗ {КОПИЯ_JS_ОТН}: ШТАМП НЕ СОВПАЛ")
        сошлось = False
    js_данные = данные_из_js(js_текст)
    if js_данные is None:
        print(f"  ✗ {КОПИЯ_JS_ОТН}: копия не разбирается")
        сошлось = False
    else:
        сошлось &= сверить(
            "js-копия: фолбэк",
            js_данные["default_fallback"],
            данные["default_fallback"],
        )
        сошлось &= сверить(
            "js-копия: вид по умолчанию",
            js_данные["default_tax_kind"],
            данные["default_tax_kind"],
        )
        сошлось &= сверить(
            "js-копия: виды", js_данные["tax_kinds"], данные["tax_kinds"]
        )
        сошлось &= сверить(
            "js-копия: группы",
            js_данные["groups"],
            [г["name"] for г in данные["groups"]],
        )

    print("ИТОГ:", "копии отвечают источнику" if сошлось else "КОПИИ РАЗОШЛИСЬ")
    return 0 if сошлось else 1


if __name__ == "__main__":
    sys.exit(main())
