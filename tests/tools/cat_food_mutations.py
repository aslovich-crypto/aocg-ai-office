#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""МУТАЦИИ CAT-FOOD ② — тип продавца, флаг подтверждения, отказ выгрузки, «документ
устарел», миграции на существующих таблицах.

⚠️ ТРИНАДЦАТЬ МУТАЦИЙ НАЗВАНЫ ЧЕКПОЙНТОМ 1 (аудит 21.09.2026) и приняты
владельцем. Сверху четыре: ⑭ и ⑮ стерегут решение Д2 — «Не указано» не имеет
права молча стать фолбэком ни при создании, ни при подтверждении; ⑯ и ⑰ —
текст отказа Р5 (пять поимённо и верное числительное, чекпойнт 2); ⑱ —
сверка счёта в разовом проходе по всем чекам; ⑲ — граница порога
«больше или равно»; ⑳ и ㉑ — проход не трогает ручной выбор.

⚠️ МУТАНТ ОБЯЗАН УПАСТЬ НА СВОЁМ ЛОВЦЕ, А НЕ ГДЕ-НИБУДЬ. Засчитывается
только если в выводе есть «FAILED <ловец>»: иначе мутация, уронившая чужой
тест по чужой причине, подтвердила бы не ту ветку (урок М7, T11).

⚠️ МУТАЦИИ СТАВЯТСЯ НА КОПИИ ДЕРЕВА: исходники не трогаются ни при каком
исходе. Свод четырёхграфный: ПОСТАВЛЕНО · ПОЙМАНО · НЕ ПОСТАВЛЕНО · НЕ МЕРЕНО —
«прибор не отработал» не засчитывается за «мутант пойман».

РЕЖИМЫ:
  ./venv/bin/python tests/tools/cat_food_mutations.py           — мутации
  ./venv/bin/python tests/tools/cat_food_mutations.py --head    — обратная
      проверка: новые сторожа на коде HEAD (без правки) — что краснеет и почему
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ЮНИТ = "tests/test_seller_type.py"
ПРОХОД = "tests/test_cat_food_pass.py"
ЖИВОЙ = "tests/pg/test_cat_food_live.py"
ТЕКСТ = "tests/test_category_confirm.py"
НОВЫЕ_СТОРОЖА = (ЮНИТ, ПРОХОД, ЖИВОЙ, ТЕКСТ)

КАТ = "app/categorization.py"
ЧЕКИ = "app/routers/receipts.py"
ВЫГРУЗКА = "app/routers/odata.py"
ПОДТВ = "app/category_confirm.py"
БАЗА = "app/database.py"
ГЕН = "scripts/cat_food_flag_pass.py"

_ALTER_ПОРОГА = (
    "            ALTER TABLE org_accounting_profile\n"
    "                ADD COLUMN IF NOT EXISTS confirm_amount_threshold NUMERIC(15,2) DEFAULT 10000;\n"
)
_ЧЕТЫРЕ_ШАГА = (
    "            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;\n"
    "            UPDATE receipts SET updated_at = created_at WHERE updated_at IS NULL;\n"
    "            ALTER TABLE receipts ALTER COLUMN updated_at SET NOT NULL;\n"
    "            ALTER TABLE receipts ALTER COLUMN updated_at SET DEFAULT NOW();\n"
)

# (имя, [(файл, было, стало), …], [ловцы «файл::тест»], зачем)
МУТАНТЫ = [
    (
        "① позиции снова побеждают название у общепита",
        [
            (
                КАТ,
                "    if тип_продавца(brand, org_text) == ОБЩЕПИТ:\n",
                "    if False and тип_продавца(brand, org_text) == ОБЩЕПИТ:\n",
            )
        ],
        [ЮНИТ + "::test_у_ресторана_позиции_проигрывают_названию"],
        "сердце дефекта: меню ресторана снова уходит в «Продукты для офиса»",
    ),
    (
        "② граница слова снята — короткие признаки ищутся подстрокой",
        [
            (
                КАТ,
                r'return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, слова)) + r")(?!\w)")',
                r'return re.compile(r"(?:" + "|".join(map(re.escape, слова)) + r")")',
            )
        ],
        [ЮНИТ + "::test_ловушки_короткого_признака_не_общепит"],
        "«бар» в «Барнаул», «кафе» в «кафедра», «pub» в «republic»",
    ),
    (
        "③ розница ушла в слой названия",
        [
            (
                КАТ,
                "    if тип_продавца(brand, org_text) == ОБЩЕПИТ:\n",
                "    if тип_продавца(brand, org_text) in (ОБЩЕПИТ, РОЗНИЦА):\n",
            )
        ],
        [ЮНИТ + "::test_у_магазина_позиции_по_прежнему_главнее"],
        "вторая половина Р1: чиним рестораны — ломаем супермаркеты",
    ),
    (
        "④ общепит при создании без флага",
        [
            (
                ЧЕКИ,
                'confirm_required = нужно_подтверждение(\n        r.org or "", parsed.get("org_brand"), r.amount, порог\n    )',
                "confirm_required = False",
            )
        ],
        [ЖИВОЙ + "::test_общепит_получает_флаг_при_любой_сумме"],
        "Р2: общепит спрашивает человека всегда",
    ),
    (
        "⑤ чек дороже порога без флага",
        [(КАТ, "    return float(amount) >= float(порог)\n", "    return False\n")],
        [
            ЖИВОЙ + "::test_чек_дороже_порога_получает_флаг",
            ЖИВОЙ + "::test_чек_дороже_порога_из_ручки_не_уезжает",
        ],
        "Р3: дорогой чек уезжает в 1С без вопроса",
    ),
    (
        "⑥ порог зашит константой вместо профиля",
        [
            (
                ПОДТВ,
                '    строка = await p.fetchrow(\n        "SELECT confirm_amount_threshold',
                '    return ПОРОГ_ПО_УМОЛЧАНИЮ\n    строка = await p.fetchrow(\n        "SELECT confirm_amount_threshold',
            )
        ],
        [ЖИВОЙ + "::test_порог_берётся_из_профиля_организации"],
        "профиль универсален: у клиента свой порог или NULL",
    ),
    (
        "⑦ проверка Р5 снята",
        [(ВЫГРУЗКА, "    if не_подтверждены:\n", "    if False:\n")],
        [
            ЖИВОЙ + "::test_неподтверждённый_чек_не_уезжает_и_отказ_бесплатный",
            ЖИВОЙ + "::test_чек_дороже_порога_из_ручки_не_уезжает",
        ],
        "неподтверждённый общепит снова уезжает в чужой учёт",
    ),
    (
        "⑧ отказ Р5 стоит после записи в журнал",
        [
            (
                ВЫГРУЗКА,
                "    не_подтверждены = category_confirm.неподтверждённые(чеки)\n",
                '    await p.execute("INSERT INTO odata_exports (org_id, report_id, user_id, outcome)'
                ' VALUES ($1,$2,$3,\'running\')", user["org_id"], id, user["id"])\n'
                "    не_подтверждены = category_confirm.неподтверждённые(чеки)\n",
            )
        ],
        [ЖИВОЙ + "::test_неподтверждённый_чек_не_уезжает_и_отказ_бесплатный"],
        "отказ перестал быть бесплатным: в журнале след несостоявшейся выгрузки",
    ),
    (
        "⑨ updated_at одной строкой с DEFAULT NOW()",
        [
            (
                БАЗА,
                _ЧЕТЫРЕ_ШАГА,
                "            ALTER TABLE receipts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();\n",
            )
        ],
        [ЖИВОЙ + "::test_updated_at_у_старых_чеков_равен_created_at"],
        "каждый выгруженный документ назавтра «устарел» — прибор краснеет везде",
    ),
    (
        "⑩ триггер двигает время от флага подтверждения",
        [
            (
                БАЗА,
                "OR OLD.vat_breakdown IS DISTINCT FROM NEW.vat_breakdown)",
                "OR OLD.vat_breakdown IS DISTINCT FROM NEW.vat_breakdown"
                " OR OLD.category_confirm_required IS DISTINCT FROM NEW.category_confirm_required)",
            )
        ],
        [ЖИВОЙ + "::test_флаг_и_отметка_человека_время_не_двигают"],
        "разовый проход «состарил» бы все выгруженные документы",
    ),
    (
        "⑪ генератор прохода кладёт категорию в SET",
        [
            (
                ГЕН,
                '"UPDATE receipts SET category_confirm_required = TRUE",',
                '"UPDATE receipts SET category_confirm_required = TRUE, category_id = NULL",',
            )
        ],
        [ПРОХОД + "::test_в_set_ровно_одно_поле_флаг"],
        "проход молча становится перекатегоризацией (Cat-4 заблокирована)",
    ),
    (
        "⑫ порог снова строкой внутри CREATE TABLE (13а.24)",
        [
            (БАЗА, _ALTER_ПОРОГА, ""),
            (
                БАЗА,
                "                    CHECK (auto_post_policy IN ('when_mapped', 'never', 'always')),\n"
                "                created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),",
                "                    CHECK (auto_post_policy IN ('when_mapped', 'never', 'always')),\n"
                "                confirm_amount_threshold NUMERIC(15,2),\n"
                "                created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),",
            ),
        ],
        [ЖИВОЙ + "::test_порог_появляется_у_уже_существующего_профиля"],
        "на свежей базе поле есть, на проде — нет, и не видно ниоткуда",
    ),
    (
        "⑬ флаг пропал из ответа чека",
        [
            (
                ЧЕКИ,
                '    д = dict(row)\n    д["можно_дозапросить"]',
                '    д = dict(row)\n    д.pop("category_confirm_required", None)\n    д["можно_дозапросить"]',
            )
        ],
        [ЖИВОЙ + "::test_флаг_доезжает_до_фронта_в_ответах_чека"],
        "признак, которого нет в ответе, экран не покажет (Р4)",
    ),
    (
        "⑭ правка «Не указано» снова проходит",
        [
            (
                ЧЕКИ,
                "    if r.category is not None and r.category.strip() == НЕ_УКАЗАНО:\n",
                "    if False:\n",
            )
        ],
        [ЖИВОЙ + "::test_правка_на_не_указано_отвергается_и_ничего_не_меняет"],
        "Д2: подтверждение превращает «не знаю» в «Прочие хозрасходы»",
    ),
    (
        "⑮ «Не указано» при создании снова уходит в фолбэк",
        [
            (
                ЧЕКИ,
                "        None\n        if category == НЕ_УКАЗАНО\n        else await resolve_category_id(p, org_id, category)",
                "        await resolve_category_id(p, org_id, category)",
            )
        ],
        [ЖИВОЙ + "::test_общепит_без_статьи_записан_пустотой_а_не_фолбэком"],
        "Д2: честное «не знаю» становится правдоподобной статьёй",
    ),
    (
        "⑯ отказ снова перечисляет все чеки без предела",
        [
            (
                ПОДТВ,
                "        for ч in по_сумме[:ПОИМЁННО]\n",
                "        for ч in по_сумме\n",
            )
        ],
        [ТЕКСТ + "::test_при_двадцати_шести_поимённо_только_пять_крупнейших"],
        "двадцать шесть чеков в одном сообщении не помещаются на экран",
    ),
    (
        "⑰ числительное ломается на одиннадцати",
        [
            (
                ПОДТВ,
                "    if n % 10 == 1 and n % 100 != 11:\n",
                "    if n % 10 == 1:\n",
            )
        ],
        [ТЕКСТ + "::test_числительные_по_правилам"],
        "«и ещё 11 чек» — текст отказа выглядит сломанным",
    ),
    (
        "⑱ проход не сверяет счёт — чужой номер выпадает молча",
        [
            (
                ГЕН,
                '"  IF (SELECT count(*) FROM receipts WHERE (%s) AND category_confirm_required)"',
                '"  IF FALSE AND (SELECT count(*) FROM receipts WHERE (%s) AND category_confirm_required)"',
            )
        ],
        [ЖИВОЙ + "::test_проход_с_чужим_номером_откатывается_целиком"],
        "проход по 124 чекам выглядит полным, а часть номеров не встала",
    ),
    (
        "⑲ граница порога: «больше или равно» снова «больше»",
        [
            (
                КАТ,
                "    return float(amount) >= float(порог)\n",
                "    return float(amount) > float(порог)\n",
            )
        ],
        [ЮНИТ + "::test_порог_спрашивает_от_себя_и_выше"],
        "чек ровно на пороге (115, 10 000,00 ₽) уходит без вопроса",
    ),
    (
        "⑳ страж ручного выбора убран из UPDATE прохода",
        [
            (
                ГЕН,
                '" WHERE id = ANY(%s) AND org_id = %d AND category_manual = FALSE"',
                '" WHERE id = ANY(%s) AND org_id = %d"',
            )
        ],
        [ЖИВОЙ + "::test_проход_не_трогает_ручной_выбор"],
        "чек, ставший ручным после выгрузки, получает флаг — решение человека тронуто",
    ),
    (
        "㉑ генератор кладёт ручной выбор в список прохода",
        [
            (
                ГЕН,
                '        if с["подтвердить"] and not с["человек_уже"]:\n',
                '        if с["подтвердить"]:\n',
            )
        ],
        [ПРОХОД + "::test_ручной_выбор_в_проход_не_попадает"],
        "проход трогает шесть чеков, где человек уже решил",
    ),
]


def _копия(откуда: str, куда: str) -> None:
    shutil.copytree(
        откуда,
        куда,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git", "venv", "__pycache__", "node_modules", "*.pyc", "dist"
        ),
    )


def прогнать(ловцы, копия: str):
    """(есть ли вердикт, вывод). Ловцы — node id pytest, одним прогоном."""
    итог = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *ловцы,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "-rfE",
        ],
        cwd=копия,
        capture_output=True,
        text=True,
        timeout=900,
    )
    вывод = итог.stdout + итог.stderr
    return (" passed" in вывод or " failed" in вывод), вывод


def _упал_ловец(ловцы, вывод: str) -> bool:
    return any(("FAILED " + л) in вывод for л in ловцы)


def мутации() -> int:
    print(
        "\nМУТАЦИИ CAT-FOOD ②: тип продавца · флаг · отказ выгрузки · устаревание · миграции\n"
    )
    with tempfile.TemporaryDirectory(prefix="catfood-mut-") as врем:
        копия = os.path.join(врем, "back")
        _копия(КОРЕНЬ, копия)
        файлы = {ф for _, правки, *_ in МУТАНТЫ for ф, *_ in правки}
        исходники = {
            ф: open(os.path.join(копия, ф), encoding="utf-8").read() for ф in файлы
        }

        все_ловцы = sorted({л for м in МУТАНТЫ for л in м[2]})
        есть, вывод = прогнать(все_ловцы, копия)
        if not есть:
            print("  ⚠️ ПРИБОР НЕ ОТРАБОТАЛ на чистой копии — мерить нечем")
            print("     " + (вывод.strip().splitlines() or ["пусто"])[-1][:300])
            return 1
        if " failed" in вывод or " error" in вывод:
            print(
                "  ⚠️ ОБРАТНЫЙ ХОД НЕ ПРОЙДЕН: ловцы красны ДО мутаций\n" + вывод[-1500:]
            )
            return 1
        print(f"  обратный ход: {len(все_ловцы)} ловцов зелены на чистой копии ✓\n")

        поставлено = поймано = 0
        непоставленные, немерено, непойманные = [], [], []
        for имя, правки, ловцы, зачем in МУТАНТЫ:
            тексты = dict(исходники)
            встал = True
            for ф, было, стало in правки:
                if тексты[ф].count(было) != 1:
                    встал = False
                    print(
                        f"  ✗ НЕ ПОСТАВЛЕН  {имя}\n      якорь в {ф} встречается {тексты[ф].count(было)} раз, нужен один"
                    )
                    break
                тексты[ф] = тексты[ф].replace(было, стало)
            if not встал:
                непоставленные.append(имя)
                continue
            for ф, т in тексты.items():
                open(os.path.join(копия, ф), "w", encoding="utf-8").write(т)
            поставлено += 1
            есть, вывод = прогнать(ловцы, копия)
            for ф, т in исходники.items():
                open(os.path.join(копия, ф), "w", encoding="utf-8").write(т)
            if not есть:
                немерено.append(имя)
                print(f"  ⚠ НЕ МЕРЕНО     {имя}")
            elif _упал_ловец(ловцы, вывод):
                поймано += 1
                print(f"  ✓ ПОЙМАН        {имя}")
            else:
                непойманные.append(имя)
                print(f"  ✗ НЕ ПОЙМАН     {имя}")
            print(
                "      ловцы: "
                + " · ".join(л.split("::")[1].replace("test_", "") for л in ловцы)
            )
            print(f"      зачем: {зачем}")

        print(
            f"\nПОСТАВЛЕНО {поставлено} · ПОЙМАНО {поймано} · "
            f"НЕ ПОСТАВЛЕНО {len(непоставленные)} · НЕ МЕРЕНО {len(немерено)}"
        )
        целы = all(
            open(os.path.join(копия, ф), encoding="utf-8").read() == т
            for ф, т in исходники.items()
        )
        print("копия возвращена в исходное" + (" ✓" if целы else " ✗ НЕТ"))
        return 0 if not (непоставленные or немерено or непойманные) and целы else 1


def против_head() -> int:
    """Новые сторожа на коде HEAD: что краснеет и почему. Код HEAD — из git,
    сторожа — из рабочего дерева."""
    print("\nОБРАТНАЯ ПРОВЕРКА: новые сторожа на коде HEAD (без правки CAT-FOOD ②)\n")
    with tempfile.TemporaryDirectory(prefix="catfood-head-") as врем:
        копия = os.path.join(врем, "head")
        os.makedirs(копия)
        архив = subprocess.run(
            ["git", "archive", "HEAD"], cwd=КОРЕНЬ, capture_output=True, check=True
        )
        subprocess.run(["tar", "-x", "-C", копия], input=архив.stdout, check=True)
        for ф in НОВЫЕ_СТОРОЖА:
            shutil.copy(os.path.join(КОРЕНЬ, ф), os.path.join(копия, ф))
        # генератор прохода — новый файл; без него его сторож краснел бы
        # на импорте, то есть по ЧУЖОЙ причине. Кладём, чтобы мерить поведение.
        shutil.copy(os.path.join(КОРЕНЬ, ГЕН), os.path.join(копия, ГЕН))
        # ⚠️ КАЖДЫЙ ФАЙЛ — ОТДЕЛЬНЫМ ПРОГОНОМ: ошибка импорта одного модуля
        # прерывает сбор целиком, и живой файл спрятался бы за соседом.
        for ф in НОВЫЕ_СТОРОЖА:
            итог = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    ф,
                    "-q",
                    "--no-header",
                    "-p",
                    "no:cacheprovider",
                    "-rA",
                    "--tb=no",
                ],
                cwd=копия,
                capture_output=True,
                text=True,
                timeout=900,
            )
            вывод = итог.stdout + итог.stderr
            print(f"── {ф}")
            for строка in вывод.splitlines():
                if строка.startswith(
                    ("PASSED", "FAILED", "ERROR")
                ) or строка.startswith("E   ImportError"):
                    print("  " + строка.replace(ф + "::", "")[:160])
                elif (
                    " passed" in строка or " failed" in строка or " error" in строка
                ) and "==" in строка:
                    print("  ИТОГ: " + строка.strip("= ")[:160])
        return 0


if __name__ == "__main__":
    sys.exit(против_head() if "--head" in sys.argv else мутации())
