# -*- coding: utf-8 -*-
"""Сторож витрины трекера: до витрины доехали ВСЕ задачи.

⚠️ ЧТО СТЕРЕЖЁТСЯ — ЧИСЛО, А НЕ ВНЕШНИЙ ВИД. Витрина читается глазами, и
потерянную задачу глазами не заметить: страница на две с лишним сотни строк,
одной меньше — одной больше. Сторож сверяет НАЙДЕНО против НАПЕЧАТАНО, ровно
как требует T11: печатать оба числа и падать при расхождении.

⚠️ И ВТОРОЕ, ИЗ ТОГО ЖЕ КЛАССА, ЧТО МОЛЧАЩИЙ ПРОПУСК (T87): сборщик, который
перестал находить ЛЮБЫЕ задачи, обязан КРАСНЕТЬ, а не выдавать пустую
красивую страницу. Пустая витрина без ошибки — та же ложь.
"""

import importlib.util
import pathlib
import re
import subprocess
import sys

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
ТРЕКЕР = КОРЕНЬ / "docs/TASKS.md"
СБОРЩИК = pathlib.Path(__file__).with_name("tracker_html.py")
ВЫХОД = pathlib.Path("/tmp/aocg-tracker.html")


def _модуль(имя):
    спец = importlib.util.spec_from_file_location(
        имя, pathlib.Path(__file__).with_name(f"{имя}.py")
    )
    м = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(м)
    return м


def main():
    беды = []
    print("\nВИТРИНА ТРЕКЕРА")

    # ⚠️ Сначала — выполнима ли проверка вообще (T87). Нет файла или нет
    # сборщика — это «НЕ ВЫПОЛНЕНА», а не «совпало».
    for имя, путь in (("трекер", ТРЕКЕР), ("сборщик", СБОРЩИК)):
        if not путь.exists():
            print(f"  ✗ НЕ НАЙДЕН {имя}: {путь}")
            return 1

    ВЫХОД.unlink(missing_ok=True)
    r = subprocess.run(
        [sys.executable, str(СБОРЩИК)], cwd=КОРЕНЬ, capture_output=True, text=True
    )
    if r.returncode != 0:
        print(f"  ✗ сборщик упал (код {r.returncode}):")
        for с in (r.stdout + r.stderr).strip().splitlines():
            print(f"      {с}")
        return 1
    if not ВЫХОД.exists():
        print(f"  ✗ сборщик отработал, но {ВЫХОД} не создан")
        return 1

    # ── НАЙДЕНО против НАПЕЧАТАНО ─────────────────────────────────────
    tracker_guard = _модуль("tracker_guard")
    найдено = len(tracker_guard.строки_задач(ТРЕКЕР.read_text(encoding="utf-8")))
    страница = ВЫХОД.read_text(encoding="utf-8")
    напечатано = len(re.findall(r"<article\b", страница))

    print(f"  НАЙДЕНО в docs/TASKS.md : {найдено}")
    print(f"  НАПЕЧАТАНО в витрине     : {напечатано}")
    if найдено != напечатано:
        беды.append(f"потеряно задач: {найдено - напечатано}")
        print(f"  ✗ РАСХОЖДЕНИЕ: {найдено - напечатано}")
    else:
        print("  ✓ совпало — ни одна задача не потерялась")

    # ── свод в шапке не расходится с содержимым ───────────────────────
    м = re.search(r"задач (\d+) ·", страница)
    if not м:
        беды.append("в шапке витрины нет числа задач")
        print("  ✗ в шапке нет числа задач — читателю не с чем сверить")
    elif int(м.group(1)) != напечатано:
        беды.append(f"шапка обещает {м.group(1)}, карточек {напечатано}")
        print(f"  ✗ шапка обещает {м.group(1)}, а карточек {напечатано}")
    else:
        print(f"  ✓ шапка обещает {м.group(1)} — столько и есть")

    if беды:
        print(f"\n  ⚠️ РАСХОЖДЕНИЙ {len(беды)}")
        return 1
    print("  ИТОГ: витрина отражает файл целиком")
    return 0


if __name__ == "__main__":
    sys.exit(main())
