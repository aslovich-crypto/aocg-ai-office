#!/usr/bin/env python3
"""ЗАМЕР ЖУРНАЛА ПРИЛОЖЕНИЯ: видно ли в контейнере сообщения уровня INFO.

ЗАЧЕМ ЭТОТ ФАЙЛ ВООБЩЕ ЕСТЬ. 10.08.2026 миграция NDS-CLEANUP ③ отработала
на проде, а обещанной строки в Deploy Logs не появилось. Полчаса ушло на то,
чтобы отличить «сделано молча» от «не выполнялось»; отличить удалось только
запросом к information_schema. Причина: uvicorn настраивает ТОЛЬКО три своих
логгера, корневой остаётся без обработчиков и с уровнем WARNING, и `info`
из `app.*` отбрасывается ДО форматирования (задача S-42, T26 случай 18).

Починка — три строки `logging.basicConfig` в `app/main.py`. Их НИЧТО не держит:
тесты зелены и без них, сборка тоже. Удалить их или дописать `force=True`
можно случайно, и узнается об этом при следующем расследовании на проде.
Поэтому проверка живёт здесь, а не в блокноте одного захода.

ЧТО ДЕЛАЕТ. Обе половины, как требует правило мутационной проверки (T11):
  ① с настройкой   — INFO обязан быть виден;
  ② без настройки  — INFO обязан пропасть (настройку снимаем МУТАЦИЕЙ
     прямо в `app/main.py` и возвращаем в `finally`);
  ③ WARNING обязан быть виден в обоих случаях — это контроль самого замера:
     если пропал и он, значит меряем не то.

Мутация засчитывается, только если она РЕАЛЬНО применилась: цель ищется
в тексте, и расхождение печатается вслух, а не пропускается молча.

ЗАПУСК (в pytest НЕ входит — правит файл приложения и возвращает на место):
    ./venv/bin/python tests/tools/logging_probe.py

БАЗУ НЕ ТРОГАЕТ: приложение только ИМПОРТИРУЕТСЯ, соединение открывается
на старте uvicorn, которого здесь нет.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ГЛАВНЫЙ = os.path.join(КОРЕНЬ, "app", "main.py")
PYTHON = sys.executable

ЦЕЛЬ = """logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
    stream=sys.stdout,
)"""

# Программа-испытание повторяет контейнер: сначала уровни журнала от uvicorn
# (ровно то, что делает `uvicorn app.main:app`), затем импорт приложения,
# затем обе строки логгером приложения.
ИСПЫТАНИЕ = (
    "import logging, logging.config\n"
    "from uvicorn.config import LOGGING_CONFIG\n"
    "logging.config.dictConfig(LOGGING_CONFIG)\n"
    "import app.main  # noqa: F401\n"
    "logging.getLogger('app.database').info('ПРОБА-INFO')\n"
    "logging.getLogger('app.database').warning('ПРОБА-WARNING')\n"
)


def прогон(метка: str) -> tuple[bool, bool]:
    готово = subprocess.run(
        [PYTHON, "-c", ИСПЫТАНИЕ],
        capture_output=True,
        text=True,
        cwd=КОРЕНЬ,
        timeout=120,
        env={**os.environ, "JWT_SECRET_KEY": "проба", "DATABASE_URL": "postgres://x/x"},
    )
    весь = готово.stdout + готово.stderr
    инфо, предупреждение = "ПРОБА-INFO" in весь, "ПРОБА-WARNING" in весь
    print(
        f"  {метка}: INFO={'виден' if инфо else 'НЕ ВИДЕН'}, "
        f"WARNING={'виден' if предупреждение else 'НЕ ВИДЕН'}"
    )
    if not предупреждение:
        хвост = весь.strip().splitlines()[-1] if весь.strip() else "пусто"
        print(f"    (испытание не отработало: {хвост})")
    return инфо, предупреждение


def main() -> int:
    оригинал = io.open(ГЛАВНЫЙ, encoding="utf-8").read()
    if ЦЕЛЬ not in оригинал:
        print("ЦЕЛЬ НЕ НАЙДЕНА в app/main.py — настройка журнала переписана.")
        print("Это не повод удалять замер: перечитать S-42, обновить шаблон,")
        print("убедиться, что без настройки INFO по-прежнему пропадает.")
        return 2

    print("ОБРАТНЫЙ КОНТРОЛЬ (настройка на месте):")
    инфо_с, предупр_с = прогон("с настройкой")
    try:
        изменённый = оригинал.replace(ЦЕЛЬ, "pass  # МУТАЦИЯ: настройка снята", 1)
        if изменённый == оригинал:
            print("ТЕКСТ НЕ ИЗМЕНИЛСЯ — мутация не применилась, замер не засчитан")
            return 2
        io.open(ГЛАВНЫЙ, "w", encoding="utf-8").write(изменённый)
        print("МУТАЦИЯ (настройка снята):")
        инфо_м, предупр_м = прогон("без настройки")
    finally:
        io.open(ГЛАВНЫЙ, "w", encoding="utf-8").write(оригинал)

    порядок = [
        ("с настройкой INFO виден", инфо_с),
        ("без настройки INFO пропал", not инфо_м),
        ("WARNING виден в обоих случаях", предупр_с and предупр_м),
    ]
    print("\nВЕРДИКТ:")
    for имя, ок in порядок:
        print(f"  {'✓' if ок else '✗'} {имя}")
    целы = all(ок for _, ок in порядок)
    print(
        "  ИТОГ:",
        "журнал приложения виден, и это доказано мутацией"
        if целы
        else "ПРОВЕРКА НЕ ЗАСЧИТАНА — чинить настройку журнала (S-42)",
    )
    return 0 if целы else 1


if __name__ == "__main__":
    sys.exit(main())
