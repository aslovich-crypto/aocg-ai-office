"""Замер РАСХОЖДЕНИЙ ЗЕРКАЛА: где двойник ПОЗВОЛИТЕЛЬНЕЕ продакшена.

ЗАЧЕМ. Класс шире правила «двойник не фильтрует за тест» (T34): там ветка
делала работу теста, здесь — НЕ делает работу продакшена. Ветка FakePool,
которая игнорирует условие, стоящее в SQL роутера (`AND org_id=$2`,
`AND user_id=$3`, `AND is_active`), делает тест бессмысленным В ОБЕ
СТОРОНЫ: он не покраснеет, если защиту снять, и не докажет, что она есть.
Найдено 07.08.2026 на удалении карты: SQL с org-scope, ветка удаляла
по одному id.

КАК СЧИТАЕТ. Берёт КАЖДЫЙ литеральный SQL из app/ (execute/fetch/fetchrow/
fetchval), нормализует пробелы и ПРОИГРЫВАЕТ порядок веток FakePool так же,
как это делает сам двойник: первое совпадение выигрывает. Дальше из WHERE
запроса вынимаются условия вида `колонка=$N` и флаги (`is_active=true`),
и проверяется, упоминается ли колонка в ТЕЛЕ выигравшей ветки.

ГРАНИЦЫ, ЧЕСТНО:
  • Упоминание колонки в теле — признак, а не доказательство: ветка может
    назвать колонку и всё равно сравнить не то. Это замер «где точно дыра»,
    а не «где точно чисто».
  • Условия внутри подзапросов (`IN (SELECT … WHERE org_id=$2)`) считаются
    наравне с внешними: для двойника разницы нет.
  • SQL, собранный из переменной или f-строки, не виден. Счётчик
    разобранных запросов в выводе — контроль охвата.
  • Условие ветки, собранное через `or`, разбирается как `and` — такая
    ветка «не выигрывает», и её запросы попадают в раздел «без ветки
    вовсе» ЛОЖНО. На 07.08.2026 так попадают ровно два запроса
    `app/database.py` (DDL и стартовая выборка), обслуживаемые общей
    веткой `startswith((...)) or "…" in q`. Раздел читать глазами.
  • Ветка может обслуживать несколько запросов; расхождение печатается
    по каждому.

ПРОВЕРКА НА ИЗВЕСТНОМ ПРИМЕРЕ (правило T26). Флаг `--selfcheck` подсовывает
заведомо дырявую пару «SQL с org_id → ветка без org_id» и требует, чтобы
замер её нашёл; молчание означает сломанный разбор, а не чистое зеркало.

ЗАПУСК:
    JWT_SECRET_KEY=любой ./venv/bin/python tests/tools/mirror_gaps.py
    ./venv/bin/python tests/tools/mirror_gaps.py --selfcheck
"""

import ast
import pathlib
import re
import sys

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
CONFTEST = КОРЕНЬ / "tests" / "conftest.py"
ИСТОЧНИКИ = sorted((КОРЕНЬ / "app").rglob("*.py"))
МЕТОДЫ = ("execute", "fetch", "fetchrow", "fetchval")

# Условия, ради которых всё затевалось: ДОСТУП И ПРИНАДЛЕЖНОСТЬ.
# `id` и `token` сюда НЕ входят намеренно: это поиск строки, а не защита,
# и почти каждая ветка ищет по ним — список утонул бы в шуме. Отсеялось
# на самопроверке: здоровая ветка помечалась дырявой из-за «id».
ЗНАЧИМЫЕ = ("org_id", "user_id", "is_active", "created_by")


def _норм(s):
    return " ".join(str(s).split())


def _sql_из_приложения():
    """[(файл, строка, метод, SQL)] — литеральные запросы приложения."""
    out = []
    for файл in ИСТОЧНИКИ:
        if "tests" in файл.parts:
            continue
        try:
            tree = ast.parse(файл.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr not in МЕТОДЫ or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append(
                    (
                        файл.relative_to(КОРЕНЬ),
                        node.lineno,
                        node.func.attr,
                        _норм(arg.value),
                    )
                )
    return out


def _ветки_fakepool():
    """{метод: [(строка, [литералы условия], текст тела)]} в порядке появления."""
    исходник = CONFTEST.read_text()
    строки = исходник.split("\n")
    tree = ast.parse(исходник)
    out = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        if cls.name != "FakePool":
            continue
        for fn in cls.body:
            if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            ветки = []
            for stmt in ast.walk(fn):
                if not isinstance(stmt, ast.If):
                    continue
                литералы = []
                for узел in ast.walk(stmt.test):
                    if (
                        isinstance(узел, ast.Call)
                        and isinstance(узел.func, ast.Attribute)
                        and узел.func.attr == "startswith"
                    ):
                        for a in узел.args:
                            эл = a.elts if isinstance(a, ast.Tuple) else [a]
                            for e in эл:
                                if isinstance(e, ast.Constant) and isinstance(
                                    e.value, str
                                ):
                                    литералы.append(("префикс", _норм(e.value)))
                    if isinstance(узел, ast.Compare) and any(
                        isinstance(op, ast.In) for op in узел.ops
                    ):
                        if isinstance(узел.left, ast.Constant) and isinstance(
                            узел.left.value, str
                        ):
                            литералы.append(("подстрока", _норм(узел.left.value)))
                if not литералы:
                    continue
                тело = "\n".join(строки[stmt.lineno - 1 : stmt.end_lineno])
                ветки.append((stmt.lineno, литералы, тело))
            if ветки:
                out[fn.name] = sorted(ветки, key=lambda b: b[0])
    return out


def _подходит(литералы, sql):
    """Так же, как сам двойник: все литералы условия должны совпасть."""
    for вид, лит in литералы:
        if вид == "префикс" and not sql.startswith(лит):
            return False
        if вид == "подстрока" and лит not in sql:
            return False
    return True


def _условия(sql):
    """Колонки из WHERE/AND, ради которых стоит сверяться."""
    найдено = []
    for колонка in re.findall(
        r"(?:WHERE|AND)\s+([a-z_]+\.)?([a-z_]+)\s*=\s*\$\d+", sql, re.I
    ):
        найдено.append(колонка[1])
    for колонка in re.findall(
        r"(?:WHERE|AND)\s+([a-z_]+)\s*=\s*(?:true|false)", sql, re.I
    ):
        найдено.append(колонка)
    return [c for c in dict.fromkeys(найдено) if c in ЗНАЧИМЫЕ]


def _упомянута(колонка, тело):
    return re.search(rf"(?<![a-z_]){re.escape(колонка)}(?![a-z_])", тело) is not None


def проверить(запросы, ветки):
    расхождения = []
    без_ветки = []
    for файл, строка, метод, sql in запросы:
        кандидаты = ветки.get(метод, [])
        победа = next((b for b in кандидаты if _подходит(b[1], sql)), None)
        if победа is None:
            без_ветки.append((файл, строка, метод, sql))
            continue
        пропущены = [c for c in _условия(sql) if not _упомянута(c, победа[2])]
        if пропущены:
            расхождения.append((файл, строка, метод, sql, победа[0], пропущены))
    return расхождения, без_ветки


def _самопроверка(ветки):
    """Известный пример: SQL с org-scope против ветки, которая его не смотрит."""
    дырявая = {
        "execute": [
            (
                0,
                [("префикс", "DELETE FROM проба WHERE id=$1")],
                "if q.startswith('DELETE FROM проба WHERE id=$1'):\n    удалить(args[0])",
            )
        ]
    }
    запрос = [
        (
            pathlib.Path("проба.py"),
            1,
            "execute",
            "DELETE FROM проба WHERE id=$1 AND org_id=$2",
        )
    ]
    расхождения, _ = проверить(запрос, дырявая)
    if not расхождения or расхождения[0][5] != ["org_id"]:
        sys.exit(
            "САМОПРОВЕРКА ПРОВАЛЕНА: заведомо дырявая пара не найдена — "
            f"разбор сломан, результату на настоящих данных верить нельзя. Получено: {расхождения}"
        )
    целая = {
        "execute": [
            (
                0,
                [("префикс", "DELETE FROM проба WHERE id=$1")],
                "if ...:\n    удалить(args[0], org_id=args[1])",
            )
        ]
    }
    ложная, _ = проверить(запрос, целая)
    if ложная:
        sys.exit(f"САМОПРОВЕРКА ПРОВАЛЕНА: ложная тревога на здоровой ветке: {ложная}")
    print("✓ самопроверка: дырявая пара найдена, здоровая не помечена")


def main():
    ветки = _ветки_fakepool()
    if "--selfcheck" in sys.argv:
        _самопроверка(ветки)
        return
    _самопроверка(ветки)

    запросы = _sql_из_приложения()
    if not запросы or not ветки:
        sys.exit("Разобрано ноль запросов или ноль веток — сломан разбор, а не «чисто»")

    расхождения, без_ветки = проверить(запросы, ветки)
    print(
        f"Разобрано запросов приложения: {len(запросы)}; "
        f"веток FakePool: {sum(len(v) for v in ветки.values())}\n"
    )
    print("── ДВОЙНИК ИГНОРИРУЕТ УСЛОВИЕ, КОТОРОЕ ЕСТЬ В ЗАПРОСЕ ──")
    for файл, строка, метод, sql, ветка, пропущены in расхождения:
        print(f"\n  {файл}:{строка} ({метод}) → conftest.py:{ветка}")
        print(f"    не смотрит: {', '.join(пропущены)}")
        print(f"    SQL: {sql[:120]}")
    print(f"\nИТОГО расхождений: {len(расхождения)}")
    if без_ветки:
        print(
            f"\n── ЗАПРОСЫ БЕЗ ВЕТКИ ВОВСЕ ({len(без_ветки)}) — упадут NotImplementedError, "
            "если ручку вызвать ──"
        )
        for файл, строка, метод, sql in без_ветки:
            print(f"  {файл}:{строка} ({метод}) {sql[:100]}")


if __name__ == "__main__":
    main()
