#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""МУТАЦИИ СТРОКИ 20 «время чека без ложной зоны» (правило T11).

⚠️ СВОД ЧЕТЫРЁХГРАФНЫЙ: ПОСТАВЛЕНО · ПОЙМАНО · НЕ ПОСТАВЛЕНО · НЕ МЕРЕНО.
«Мутант не пойман» и «прибор не отработал» — разные беды, и вторая опаснее:
она показывает проверку там, где её не было.

⚠️ ЧТО ИМЕННО СТЕРЕЖЁМ. Правка живёт в ДВУХ РЕПОЗИТОРИЯХ и неразделима:
  М1 — `USING ... AT TIME ZONE 'UTC'` убран из DDL. Голый ALTER конвертирует
       ПО ПОЯСУ СЕССИИ и отрабатывает без единой ошибки; на прод-кластере
       (UTC) результат верен, на машине разработчика (Europe/Moscow) все
       чеки уезжают на три часа. Класс: правка верна при НЕНАЗВАННОМ условии.
  М2 — компенсация во фронте снята, а тип колонки прежний: экран уезжает
       ВПЕРЁД и на сутки (21:34 → 00:34 назавтра).
  М3 — тип сменён, а компенсация осталась: экран уезжает НАЗАД (21:34 → 18:34).
М2 и М3 — про ПОРЯДОК, а не про код: каждая половина по отдельности хуже,
чем ни одной, и обе молчат.

⚠️ МУТАЦИИ СТАВЯТСЯ НА КОПИИ ОБОИХ ДЕРЕВЬЕВ. Исходники не трогаются ни при
каком исходе, включая падение самого рунера.

ЗАПУСК: ./venv/bin/python tests/tools/receipt_time_mutations.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ФРОНТ = os.path.normpath(os.path.join(КОРЕНЬ, "..", "aocg-ai-office-web"))

DDL_С_USING = (
    "ALTER TABLE receipts\n"
    "                  ALTER COLUMN datetime TYPE TIMESTAMP WITHOUT TIME ZONE\n"
    "                  USING datetime AT TIME ZONE 'UTC'"
)

# (имя, репозиторий, файл, было, стало, ловцы, зачем)
# Ловец: ("pytest", "путь::тест") либо ("сторож",) — парный сторож, ждём код 1.
МУТАНТЫ = [
    (
        "М1 USING убран — конвертация поедет по поясу сессии",
        "бэк",
        "app/database.py",
        # ⚠️ ЯКОРЬ С ОТСТУПОМ: голая строка встречается ДВАЖДЫ — второй раз
        # в объяснении над DDL. Мутация по неоднозначному якорю не ставится
        # вовсе, и это читалось бы как «прибор промолчал».
        "\n                  USING datetime AT TIME ZONE 'UTC';",
        ";",
        [("pytest", "tests/test_migration_receipt_datetime.py")],
        "⚠️ ЛОВЕЦ ЗДЕСЬ ТОЛЬКО СТРУКТУРНЫЙ, И ЭТО ЗАМЕР, А НЕ НЕДОСМОТР. "
        "Прогон 11.09.2026 показал: живой контур на эту мутацию МОЛЧИТ, "
        "потому что DDL миграции выполняется в init_db(), когда таблица "
        "ПУСТА — сдвигать нечего. Проверка на данных возможна только там, "
        "где данные есть: см. М1б",
    ),
    (
        "М1б USING убран из РЕЛЬСОВ — валидация перестала быть валидацией",
        "бэк",
        "scripts/validate_receipt_datetime.py",
        "f\"TYPE TIMESTAMP WITHOUT TIME ZONE USING {КОЛОНКА} AT TIME ZONE 'UTC'\",",
        'f"TYPE TIMESTAMP WITHOUT TIME ZONE",',
        [
            (
                "pytest",
                "tests/pg/test_migration_receipt_datetime_live.py",
            ),
            ("pytest", "tests/test_migration_receipt_datetime.py"),
        ],
        "рельсы — источник строки DDL и для init_db, и для SQL бастиона; "
        "живой прибор кладёт два чека на краях суток и сверяет сдвиг "
        "при поясе Europe/Moscow, где голый ALTER уносит время на три часа",
    ),
    (
        "М2 компенсация снята, тип НЕ менялся — экран уедет вперёд на сутки",
        "бэк",
        "app/database.py",
        DDL_С_USING,
        "NULL",
        [("сторож",)],
        "половина ② без половины ①: порядок неразделим",
    ),
    (
        "М3 тип сменён, компенсация ОСТАЛАСЬ — экран уедет назад на три часа",
        "фронт",
        "src/lib/format.js",
        '  return d.toLocaleString("ru-RU", {\n    day: "2-digit",',
        '  return d.toLocaleString("ru-RU", {\n    timeZone: "UTC",\n    day: "2-digit",',
        [("сторож",)],
        "половина ① без половины ②: та же неразделимость с другого конца",
    ),
]


def прогнать(ловец, копия_бэка: str, копия_фронта: str):
    """(есть ли вердикт, «прошёл»/«упал», вывод)."""
    if ловец[0] == "pytest":
        итог = subprocess.run(
            [sys.executable, "-m", "pytest", ловец[1], "-q", "--no-header"],
            cwd=копия_бэка,
            capture_output=True,
            text=True,
            timeout=600,
        )
        вывод = итог.stdout + итог.stderr
        есть = " passed" in вывод or " failed" in вывод
        прошёл = " failed" not in вывод and " error" not in вывод
        return есть, ("прошёл" if прошёл else "упал"), вывод

    итог = subprocess.run(
        [sys.executable, "tests/tools/receipt_time_pair.py"],
        cwd=копия_бэка,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "FRONT_SRC": os.path.join(копия_фронта, "src")},
    )
    вывод = итог.stdout + итог.stderr
    # ⚠️ КОД 2 — «фронт недоступен», это НЕ вердикт. Без различения пропуск
    # засчитался бы как «мутант не пойман».
    есть = "ИТОГ:" in вывод and итог.returncode in (0, 1)
    return есть, ("прошёл" if итог.returncode == 0 else "упал"), вывод


def main() -> int:
    print("\nМУТАЦИИ СТРОКИ 20: тип колонки · USING · неразделимость порядка\n")
    if not os.path.isdir(ФРОНТ):
        print(f"  ⚠️ ЗАМЕР НЕ ВЫПОЛНЕН: нет фронта ({ФРОНТ}) — мутации М3 негде ставить")
        return 1

    with tempfile.TemporaryDirectory(prefix="s20-mut-") as врем:
        игнор = shutil.ignore_patterns(
            ".git", "venv", "__pycache__", "node_modules", "*.pyc", "dist"
        )
        копия_бэка = os.path.join(врем, "back")
        копия_фронта = os.path.join(врем, "front")
        shutil.copytree(КОРЕНЬ, копия_бэка, symlinks=True, ignore=игнор)
        shutil.copytree(ФРОНТ, копия_фронта, symlinks=True, ignore=игнор)

        корни = {"бэк": копия_бэка, "фронт": копия_фронта}
        исходники = {
            (р, ф): open(os.path.join(корни[р], ф), encoding="utf-8").read()
            for _, р, ф, *_ in МУТАНТЫ
        }

        # ОБРАТНЫЙ ХОД: на нетронутых копиях все ловцы зелены.
        ловцы = []
        for м in МУТАНТЫ:
            for л in м[5]:
                if л not in ловцы:
                    ловцы.append(л)
        for л in ловцы:
            есть, исход, вывод = прогнать(л, копия_бэка, копия_фронта)
            метка = л[1] if л[0] == "pytest" else "парный сторож"
            if not есть:
                print(f"  ⚠️ ПРИБОР НЕ ОТРАБОТАЛ на чистом дереве ({метка})")
                print("     " + (вывод.strip().splitlines() or ["пусто"])[-1][:200])
                return 1
            if исход != "прошёл":
                print(f"  ⚠️ ОБРАТНЫЙ ХОД НЕ ПРОЙДЕН: {метка} красен ДО мутаций")
                print(вывод[-1200:])
                return 1
        print(f"  обратный ход: {len(ловцы)} ловца зелены на нетронутых копиях ✓\n")

        поставлено = поймано = 0
        непоставленные: list[str] = []
        немерено: list[str] = []
        for имя, репо, файл, было, стало, ловцы_м, зачем in МУТАНТЫ:
            путь = os.path.join(корни[репо], файл)
            исходный = исходники[(репо, файл)]
            if исходный.count(было) != 1:
                непоставленные.append(имя)
                print(f"  ✗ НЕ ПОСТАВЛЕН  {имя}")
                print(f"      якорь встречается {исходный.count(было)} раз, нужен один")
                continue
            open(путь, "w", encoding="utf-8").write(исходный.replace(было, стало))
            поставлено += 1
            пойман, отчёт = False, []
            for л in ловцы_м:
                есть, исход, _ = прогнать(л, копия_бэка, копия_фронта)
                метка = л[1].split("::")[-1] if л[0] == "pytest" else "парный сторож"
                if not есть:
                    отчёт.append(f"{метка}: ПРИБОР НЕ ОТРАБОТАЛ")
                    continue
                отчёт.append(
                    f"{метка}: {'смолчал' if исход == 'прошёл' else 'КРАСНЕЕТ'}"
                )
                пойман = пойман or исход == "упал"
            open(путь, "w", encoding="utf-8").write(исходный)
            if пойман:
                поймано += 1
                print(f"  ✓ ПОЙМАН        {имя}")
            elif all("НЕ ОТРАБОТАЛ" in о for о in отчёт):
                немерено.append(имя)
                print(f"  ⚠ НЕ МЕРЕНО     {имя}")
            else:
                print(f"  ✗ НЕ ПОЙМАН     {имя}")
            print(f"      {' · '.join(отчёт)}")
            print(f"      зачем: {зачем}")

        print(
            f"\nПОСТАВЛЕНО {поставлено} · ПОЙМАНО {поймано} · "
            f"НЕ ПОСТАВЛЕНО {len(непоставленные)} · НЕ МЕРЕНО {len(немерено)}"
        )
        целы = all(
            open(os.path.join(корни[р], ф), encoding="utf-8").read() == т
            for (р, ф), т in исходники.items()
        )
        print("копии возвращены в исходное" + (" ✓" if целы else " ✗ НЕТ"))
        плохо = len(непоставленные) + len(немерено) + (поставлено - поймано)
        return 0 if плохо == 0 and целы else 1


if __name__ == "__main__":
    sys.exit(main())
