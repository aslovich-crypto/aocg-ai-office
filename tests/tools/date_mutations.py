#!/usr/bin/env python3
"""МУТАЦИИ ПРОВЕРКИ ДАТЫ ЧЕКА (строка 24, правило T11).

Проверка заявляет ЧЕТЫРЕ способности: ловить слишком старую дату, ловить
дату из будущего, НЕ трогать нормальную (допуск на часовые пояса) и называть
саму дату в тексте. Плюс пятая, отдельная: предупреждение должно доехать
до ответа ручки, а не остаться в чистой функции.

Каждая ломается ОТДЕЛЬНО. Двум веткам сообщения — своя мутация каждой:
первый прогон это и показал, M4 ломала дату в сообщении о БУДУЩЕМ, а тесты
оставались зелёными, потому что про дату спрашивал только тест СТАРОЙ ветки.
Один и тот же заявленный признак проверялся в одной ветке из двух — ровно то,
ради чего мутации и гоняются.

ЗАПУСК:  ./venv/bin/python tests/tools/date_mutations.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATE_CHECK = ROOT / "app" / "date_check.py"
OCR = ROOT / "app" / "routers" / "ocr.py"
UNIT = "tests/test_date_check.py"
API = "tests/test_api.py"

MUTATIONS = [
    (
        "M1 порог возраста снят (год превращается в век)",
        DATE_CHECK,
        "MAX_AGE_DAYS = 365",
        "MAX_AGE_DAYS = 36500",
        "test_real_case_from_prod_is_caught",
        UNIT,
    ),
    (
        "M2 проверка будущего убрана",
        DATE_CHECK,
        "    if value > today + timedelta(days=FUTURE_TOLERANCE_DAYS):",
        "    if False:",
        "test_future_date_is_caught",
        UNIT,
    ),
    (
        "M3 допуск на часовые пояса убран (ложная тревога на нормальный чек)",
        DATE_CHECK,
        "FUTURE_TOLERANCE_DAYS = 1",
        "FUTURE_TOLERANCE_DAYS = 0",
        "test_one_day_ahead_is_tolerated_because_of_time_zones",
        UNIT,
    ),
    (
        "M4 дата пропала из текста про БУДУЩЕЕ",
        DATE_CHECK,
        '"ошибка распознавания." % value.strftime("%d.%m.%Y")',
        '"ошибка распознавания." % ""',
        "test_future_date_is_caught",
        UNIT,
    ),
    (
        "M5 дата пропала из текста про СТАРЫЙ чек",
        DATE_CHECK,
        '"ошибка распознавания." % (value.strftime("%d.%m.%Y"), сколько)',
        '"ошибка распознавания." % ("", сколько)',
        "test_warning_names_the_date_so_it_can_be_checked",
        UNIT,
    ),
    (
        "M6 предупреждение не доезжает до ответа ручки",
        OCR,
        '        if warning:\n            out["warnings"].append(warning)',
        "        if warning:\n            pass",
        "test_ocr_warns_about_implausible_date",
        API,
    ),
]


def run(target: str):
    proc = subprocess.run(
        [str(ROOT / "venv" / "bin" / "python"), "-m", "pytest", target, "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    code, _ = run(UNIT)
    if code != 0:
        print("ИСХОДНЫЙ ПРОГОН КРАСНЫЙ — мутации бессмысленны")
        return 1
    print("исходный прогон зелёный, начинаем ломать\n")

    failures = []
    for name, path, old, new, expected, target in MUTATIONS:
        src = path.read_text(encoding="utf-8")
        if src.count(old) != 1:
            print(
                "✗ %s — ЦЕЛЬ НЕ НАЙДЕНА (вхождений %d), мутация НЕ ВЫПОЛНЕНА"
                % (name, src.count(old))
            )
            failures.append(name)
            continue
        path.write_text(src.replace(old, new), encoding="utf-8")
        try:
            code, out = run(target)
        finally:
            path.write_text(src, encoding="utf-8")
        if code == 0:
            print("✗ %s — тесты остались зелёными" % name)
            failures.append(name)
        elif expected not in out:
            print(
                "✗ %s — красный НЕ ПО ТОЙ ПРИЧИНЕ: в выводе нет %r" % (name, expected)
            )
            failures.append(name)
        else:
            print("✓ %s — поймано (%s)" % (name, expected))

    code, _ = run(UNIT)
    print(
        "\nвозврат:", "прогон снова зелёный" if code == 0 else "КРАСНЫЙ — разбираться"
    )
    print("мутаций: %d, поймано: %d" % (len(MUTATIONS), len(MUTATIONS) - len(failures)))
    return 1 if failures or code != 0 else 0


if __name__ == "__main__":
    sys.exit(main())
