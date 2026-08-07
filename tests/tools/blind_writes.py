"""Замер СЛЕПЫХ ЗАПИСЕЙ: где UPDATE/DELETE не смотрит, задел ли он строку.

ЗАЧЕМ. Ручка, которая выполняет UPDATE/DELETE и отвечает одинаково
и когда строка изменена, и когда её не нашлось, НЕ МОЖЕТ БЫТЬ ПРОВЕРЕНА
КОДОМ ОТВЕТА — ни тестом, ни руками, ни фронтом. Найдено 07.08.2026
на `DELETE /api/invite/{token}`: он отвечает `{"ok": true}` всегда,
и единственный признак работающего org-scope — состояние чужого
приглашения. Это класс, а не частность, — отсюда замер.

ЧТО СЧИТАЕТСЯ СЛЕПОЙ ЗАПИСЬЮ: результат `execute("UPDATE …")` или
`execute("DELETE …")` отброшен (вызов стоит отдельным выражением),
и в хендлере нет ни одного `raise HTTPException` — то есть отличить
«сделано» от «нечего было делать» ручка не пытается нигде.

ГРАНИЦЫ, ЧЕСТНО:
  • Проверка существования может стоять ДО записи (SELECT + 404) —
    такие ручки различают случаи и сюда не попадают. Наличие `raise`
    считается признаком такой проверки; хендлер, который бросает 403
    по роли, но не проверяет строку, разбор пометит «частично» —
    смотреть глазами.
  • Разбор видит только литеральные SQL-строки в самом хендлере.
    Запрос, собранный в переменной или вынесенный в сервис, не
    попадёт в счёт: счётчик «разобрано» покажет просадку.
  • Это не приговор ручке. Иногда «ok всегда» — осознанный выбор
    (идемпотентное гашение). Замер отвечает на вопрос «сколько таких
    и какие», а не «что чинить».

ЗАПУСК:
    JWT_SECRET_KEY=любой ./venv/bin/python tests/tools/blind_writes.py
"""

import ast
import pathlib
import sys

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
РОУТЕРЫ = sorted((КОРЕНЬ / "app" / "routers").glob("*.py"))


def _эндпоинт(fn):
    """«POST /api/invite/create» из декораторов, либо None."""
    for d in fn.decorator_list:
        if not isinstance(d, ast.Call) or not isinstance(d.func, ast.Attribute):
            continue
        метод = d.func.attr.upper()
        if метод not in ("GET", "POST", "PATCH", "PUT", "DELETE"):
            continue
        путь = ""
        if d.args and isinstance(d.args[0], ast.Constant):
            путь = d.args[0].value
        return f"{метод} {путь}"
    return None


def _записи(fn):
    """[(вид SQL, результат использован?)] по вызовам execute внутри."""
    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "execute":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        sql = " ".join(str(node.args[0].value).split()).upper()
        вид = next(
            (k for k in ("UPDATE", "DELETE", "INSERT") if sql.startswith(k)), None
        )
        if вид:
            found.append((вид, node))
    return found


def main():
    строки = []
    разобрано = 0
    for файл in РОУТЕРЫ:
        # ОДИН разбор на файл: id() узлов сравнимы только внутри одного
        # дерева. Первая версия парсила файл дважды — множество «отброшенных»
        # не совпало с узлами второго дерева, и замер честно выдал НОЛЬ
        # слепых записей при заведомо известной одной. Ровно тот случай,
        # ради которого в шапке стоит отказ при нулевом разборе.
        tree = ast.parse(файл.read_text())
        отброшенные = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr):
                вн = node.value
                if isinstance(вн, ast.Await):
                    вн = вн.value
                if isinstance(вн, ast.Call):
                    отброшенные.add(id(вн))

        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            точка = _эндпоинт(fn)
            if not точка:
                continue
            записи = _записи(fn)
            разобрано += len(записи)
            слепые = [
                вид
                for вид, вызов in записи
                if вид in ("UPDATE", "DELETE") and id(вызов) in отброшенные
            ]
            if not слепые:
                continue
            бросает = any(
                isinstance(n, ast.Raise)
                and isinstance(n.exc, ast.Call)
                and getattr(n.exc.func, "id", "") == "HTTPException"
                for n in ast.walk(fn)
            )
            строки.append(
                (
                    файл.name,
                    fn.lineno,
                    точка,
                    "+".join(sorted(set(слепые))),
                    "есть проверки (смотреть глазами)" if бросает else "СЛЕПАЯ ЗАПИСЬ",
                )
            )

    if разобрано == 0:
        sys.exit(
            "Разобрано НОЛЬ записей — сломался разбор, а не «всё чисто». "
            "Проверьте, не собираются ли запросы из переменных."
        )

    print(f"Разобрано записывающих вызовов: {разобрано}\n")
    for имя, стр, точка, вид, вердикт in sorted(строки, key=lambda x: (x[4], x[0])):
        print(f"  {вердикт:34} {вид:13} {точка:38} {имя}:{стр}")
    слепых = sum(1 for r in строки if r[4] == "СЛЕПАЯ ЗАПИСЬ")
    print(f"\nИТОГО: слепых {слепых}, под вопросом {len(строки) - слепых}")


if __name__ == "__main__":
    main()
