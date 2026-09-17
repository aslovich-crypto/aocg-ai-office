#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""МУТАЦИИ МИГРАЦИИ «таблицы обмена с 1С» (1C-21 заход ②, правило T11).

⚠️ ЧТО СТЕРЕЖЁМ. Схема БД — место, где ошибка дороже всего: она уезжает
на прод при СЛЕДУЮЩЕМ СТАРТЕ контейнера и чинится уже на живых данных.
Три беды здесь тихие: список рельсов разошёлся с `init_db` (проверяли одно,
применили другое), обратный DDL забыл индекс (повторный накат даст конфликт
имён), и защита от второго документа выражена не тем индексом.

⚠️ ГРАНИЦА: мутации НЕ проверяют, что миграция применится на проде. Это
делает владелец прогоном BEGIN/ROLLBACK на бастионе, а потом сам `init_db`.

⚠️ МУТАЦИИ СТАВЯТСЯ НА КОПИИ ДЕРЕВА. Исходники не трогаются ни при каком
исходе, включая падение рунера.

ЗАПУСК: make odata-tables-mut · ./venv/bin/python tests/tools/odata_tables_mutations.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
РЕЛЬСЫ = "scripts/validate_odata_tables.py"
СХЕМА = "app/database.py"
СТОРОЖ = "tests/test_migration_odata_tables.py"
ЖИВОЙ = "tests/pg/test_migration_odata_tables_live.py"

# (имя, файл, было, стало, ловцы, зачем)
МУТАНТЫ = [
    (
        "⑬ CHECK на ОтражениеВУСН снят — в базу лёг бы любой текст",
        РЕЛЬСЫ,
        """    usn_reflection TEXT NOT NULL DEFAULT 'Принимаются'
        CHECK (usn_reflection IN ('Принимаются', 'НеПринимаются',
                                  'Распределяются')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),""",
        """    usn_reflection TEXT NOT NULL DEFAULT 'Принимаются',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),""",
        [ЖИВОЙ],
        "поле документа в 1С объявлено строкой и опечатку не отобьёт: "
        "«принимаются» с маленькой буквы уехало бы в чужой учёт молча",
    ),
    (
        "⑭ статья затрат снова необязательна",
        СХЕМА,
        "                expense_ref   TEXT NOT NULL,\n                expense_name  TEXT,\n                usn_reflection TEXT NOT NULL DEFAULT 'Принимаются'\n                    CHECK (usn_reflection IN ('Принимаются', 'НеПринимаются',\n                                              'Распределяются')),\n                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n                updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()\n            );\n            -- Одна категория — одно переопределение внутри организации.\n            CREATE UNIQUE INDEX IF NOT EXISTS org_category_map_unique\n                ON org_category_map(org_id, category_id);\n            -- ⚠️ ОТДЕЛЬНЫМИ ALTER, И ЭТО НЕ ИЗБЫТОЧНОСТЬ: `CREATE TABLE IF NOT\n            -- EXISTS` на уже созданной таблице не добавляет ни колонок,\n            -- ни ограничений. На проде таблица создана заходом ②, и правки\n            -- приедут туда только так.\n            ALTER TABLE org_category_map\n                ADD COLUMN IF NOT EXISTS usn_reflection TEXT NOT NULL DEFAULT 'Принимаются'\n                    CHECK (usn_reflection IN ('Принимаются', 'НеПринимаются',\n                                              'Распределяются'));\n            ALTER TABLE org_category_map ALTER COLUMN account_code DROP NOT NULL;\n            ALTER TABLE org_category_map ALTER COLUMN expense_ref SET NOT NULL;",
        "                expense_ref   TEXT,\n                expense_name  TEXT,\n                usn_reflection TEXT NOT NULL DEFAULT 'Принимаются'\n                    CHECK (usn_reflection IN ('Принимаются', 'НеПринимаются',\n                                              'Распределяются')),\n                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n                updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()\n            );\n            -- Одна категория — одно переопределение внутри организации.\n            CREATE UNIQUE INDEX IF NOT EXISTS org_category_map_unique\n                ON org_category_map(org_id, category_id);\n            -- ⚠️ ОТДЕЛЬНЫМИ ALTER, И ЭТО НЕ ИЗБЫТОЧНОСТЬ: `CREATE TABLE IF NOT\n            -- EXISTS` на уже созданной таблице не добавляет ни колонок,\n            -- ни ограничений. На проде таблица создана заходом ②, и правки\n            -- приедут туда только так.\n            ALTER TABLE org_category_map\n                ADD COLUMN IF NOT EXISTS usn_reflection TEXT NOT NULL DEFAULT 'Принимаются'\n                    CHECK (usn_reflection IN ('Принимаются', 'НеПринимаются',\n                                              'Распределяются'));\n            ALTER TABLE org_category_map ALTER COLUMN account_code DROP NOT NULL;\n            SELECT 1;",
        [СТОРОЖ, ЖИВОЙ],
        "⚠️ ЛОВИТ СТОРОЖ РАСХОЖДЕНИЯ, А НЕ ЖИВОЙ ПРОГОН, и это замер: в живом "
        "контуре схему восстанавливают РЕЛЬСЫ, и NOT NULL возвращается оттуда. "
        "NOT NULL объявлен дважды — в CREATE и отдельным ALTER для баз, "
        "созданных прежней редакцией: снятие ОДНОГО места ничего не меняет. "
        "Правило без статьи — не правило: документ уедет со статьёй, которую "
        "подставит сама 1С",
    ),
    (
        "⑮ профиль организации перестал быть единственным",
        РЕЛЬСЫ,
        "CREATE UNIQUE INDEX IF NOT EXISTS org_accounting_profile_unique",
        "CREATE INDEX IF NOT EXISTS org_accounting_profile_unique",
        [ЖИВОЙ],
        "два профиля у организации означают, что состав документа зависит "
        "от порядка выборки — тот же класс, что два правила на категорию",
    ),
    (
        "⑯ CHECK на режимы профиля снят",
        РЕЛЬСЫ,
        """    vat_mode             TEXT NOT NULL
        CHECK (vat_mode IN ('not_payer', 'included', 'deductible')),""",
        """    vat_mode             TEXT NOT NULL,""",
        [ЖИВОЙ],
        "опечатка в режиме НДС тихо меняет состав документа: при included "
        "чек не дробится, при deductible дробится и требует контрагента",
    ),
    (
        "① рельсы разошлись с init_db: колонка только в рельсах",
        РЕЛЬСЫ,
        "    account_code  TEXT,\n    expense_ref   TEXT NOT NULL,",
        "    account_code  TEXT,\n    lишняя_колонка TEXT,\n    expense_ref   TEXT NOT NULL,",
        [СТОРОЖ],
        "проверяли бы одно, а применяли другое: валидация зелёная, а на прод "
        "уедет ДРУГОЙ DDL — ровно тот класс, ради которого сторож и заведён",
    ),
    (
        "② обратный DDL забыл индекс",
        РЕЛЬСЫ,
        '    "DROP INDEX IF EXISTS odata_exports_one_success",\n',
        "",
        [СТОРОЖ],
        "точка отката, забывшая индекс, оставляет мусор: повторный накат даст "
        "конфликт имён, и разбираться будут на проде",
    ),
    (
        "③ уникальность успеха стала полной, а не частичной",
        РЕЛЬСЫ,
        "    ON odata_exports(report_id) WHERE outcome = 'ok'",
        "    ON odata_exports(report_id)",
        [СТОРОЖ, ЖИВОЙ],
        "запрет лёг бы и на повторные ПОПЫТКИ: вторая неудачная выгрузка "
        "упала бы на индексе, и журнал перестал бы быть журналом",
    ),
    (
        "④ одна категория получила право на два счёта",
        РЕЛЬСЫ,
        "    ON org_category_map(org_id, category_id)",
        "    ON org_category_map(org_id, category_id, account_code)",
        [СТОРОЖ, ЖИВОЙ],
        "два правила на одну строку расхода — и проводка зависит от порядка "
        "выборки; такой дефект не ловится ничем, кроме этого индекса",
    ),
    (
        "⑤ в валидации появилось закрепление",
        РЕЛЬСЫ,
        '    print("ROLLBACK;")',
        '    print("COMMIT;")',
        [СТОРОЖ],
        "печатный SQL уходит на бастион и выполняется там: один COMMIT "
        "превращает проверку в тихую миграцию мимо всякого решения",
    ),
    (
        "⑥ DDL печатается один раз — идемпотентность не проверяется",
        РЕЛЬСЫ,
        '    print("-- повтор целиком: init_db выполняется на КАЖДОМ старте контейнера")\n    for команда in МИГРАЦИЯ:\n        print(команда.strip() + ";")',
        "",
        [СТОРОЖ],
        "ошибка идемпотентности не видна на пустой базе и роняет приложение "
        "на ВТОРОМ старте — уже на проде",
    ),
    (
        "⑦ внешний ключ на организацию снят",
        РЕЛЬСЫ,
        "    org_id        INTEGER NOT NULL REFERENCES organizations(id),",
        "    org_id        INTEGER NOT NULL,",
        [СТОРОЖ],
        "у старых receipts и reports org_id без ключа — это долг, и повторять "
        "его в новых таблицах незачем",
    ),
    (
        "⑨ перечень подстановок заменён флагом «были подстановки»",
        РЕЛЬСЫ,
        """    defaulted_categories TEXT[] NOT NULL DEFAULT '{}'""",
        "    defaulted_used BOOLEAN NOT NULL DEFAULT false",
        [СТОРОЖ, ЖИВОЙ],
        "флаг скажет «подстановки были», но не скажет КАКИЕ КАТЕГОРИИ "
        "настроить — а через месяц спросят именно это",
    ),
    (
        "⑩ подстановки допускают неизвестность: NOT NULL снят",
        РЕЛЬСЫ,
        """    defaulted_categories TEXT[] NOT NULL DEFAULT '{}'""",
        "    defaulted_categories TEXT[]",
        [ЖИВОЙ],
        "NULL означал бы «не знаем, были ли подстановки» — ровно то "
        "состояние, ради ухода от которого колонка и заведена",
    ),
    (
        "⑫ драйвер базы снова импортируется вверху рельсов",
        РЕЛЬСЫ,
        "import asyncio\nimport os\nimport sys\n",
        "import asyncio\nimport os\nimport sys\n\nimport asyncpg\n",
        [СТОРОЖ],
        "`--sql` владелец запускает на машине без asyncpg; импорт вверху ронял "
        "печать SQL ещё до текста — прибор нельзя было запустить там, где он нужен",
    ),
    (
        "⑧ таблицы в init_db нет вовсе, а рельсы зелёные",
        СХЕМА,
        "            CREATE TABLE IF NOT EXISTS odata_exports (",
        "            CREATE TABLE IF NOT EXISTS odata_exports_НЕ_ТА (",
        [СТОРОЖ, ЖИВОЙ],
        "рельсы создают таблицы СВОИМ списком и прошли бы, даже если бы "
        "в init_db блока не было вовсе: зелёная валидация не доказывает, "
        "что миграция уедет на прод",
    ),
]


def прогнать(файл_тестов: str, копия: str):
    итог = subprocess.run(
        [sys.executable, "-m", "pytest", файл_тестов, "-q", "--no-header"],
        cwd=копия,
        capture_output=True,
        text=True,
        timeout=900,
    )
    вывод = итог.stdout + итог.stderr
    есть = " passed" in вывод or " failed" in вывод
    прошёл = " failed" not in вывод and " error" not in вывод
    return есть, ("прошёл" if прошёл else "упал"), вывод


def main() -> int:
    print("\nМУТАЦИИ МИГРАЦИИ 1С: список · откат · индексы · идемпотентность\n")
    with tempfile.TemporaryDirectory(prefix="odata-tables-mut-") as врем:
        копия = os.path.join(врем, "back")
        shutil.copytree(
            КОРЕНЬ,
            копия,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git", "venv", "__pycache__", "node_modules", "*.pyc", "dist"
            ),
        )
        файлы = sorted({м[1] for м in МУТАНТЫ})
        исходники = {
            ф: open(os.path.join(копия, ф), encoding="utf-8").read() for ф in файлы
        }

        # ОБРАТНЫЙ ХОД: на нетронутой копии оба ловца зелены.
        for ловец in (СТОРОЖ, ЖИВОЙ):
            есть, исход, вывод = прогнать(ловец, копия)
            if not есть:
                print("  ⚠️ ПРИБОР НЕ ОТРАБОТАЛ на чистом дереве (%s)" % ловец)
                print("     " + (вывод.strip().splitlines() or ["пусто"])[-1][:300])
                return 1
            if исход != "прошёл":
                print("  ⚠️ ОБРАТНЫЙ ХОД НЕ ПРОЙДЕН: %s красен ДО мутаций" % ловец)
                print(вывод[-1500:])
                return 1
        print("  обратный ход: оба ловца зелены на чистой копии ✓\n")

        поставлено = поймано = 0
        непоставленные: list[str] = []
        немерено: list[str] = []
        for имя, файл, было, стало, ловцы, зачем in МУТАНТЫ:
            путь = os.path.join(копия, файл)
            исходный = исходники[файл]
            if исходный.count(было) != 1:
                непоставленные.append(имя)
                print("  ✗ НЕ ПОСТАВЛЕН  %s" % имя)
                print(
                    "      якорь встречается %d раз, нужен один" % исходный.count(было)
                )
                continue
            open(путь, "w", encoding="utf-8").write(исходный.replace(было, стало))
            поставлено += 1
            пойман, отчёт = False, []
            for ловец in ловцы:
                есть, исход, _ = прогнать(ловец, копия)
                отчёт.append(
                    "%s: %s"
                    % (
                        os.path.basename(ловец),
                        "ПРИБОР НЕ ОТРАБОТАЛ"
                        if not есть
                        else ("смолчал" if исход == "прошёл" else "КРАСНЕЕТ"),
                    )
                )
                пойман = пойман or (есть and исход == "упал")
            open(путь, "w", encoding="utf-8").write(исходный)
            if пойман:
                поймано += 1
                print("  ✓ ПОЙМАН        %s" % имя)
            elif all("НЕ ОТРАБОТАЛ" in о for о in отчёт):
                немерено.append(имя)
                print("  ⚠ НЕ МЕРЕНО     %s" % имя)
            else:
                print("  ✗ НЕ ПОЙМАН     %s" % имя)
            print("      %s" % " · ".join(отчёт))
            print("      ломает: %s" % зачем)

        print(
            "\nПОСТАВЛЕНО %d · ПОЙМАНО %d · НЕ ПОСТАВЛЕНО %d · НЕ МЕРЕНО %d"
            % (поставлено, поймано, len(непоставленные), len(немерено))
        )
        целы = all(
            open(os.path.join(копия, ф), encoding="utf-8").read() == т
            for ф, т in исходники.items()
        )
        print("копия возвращена в исходное" + (" ✓" if целы else " ✗ НЕТ"))
        плохо = len(непоставленные) + len(немерено) + (поставлено - поймано)
        return 0 if плохо == 0 and целы else 1


if __name__ == "__main__":
    sys.exit(main())
