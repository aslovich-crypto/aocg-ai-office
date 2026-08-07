"""Сторож порядка веток FakePool (задача T6).

ЗАЧЕМ. FakePool в conftest.py разбирает запрос вручную: длинная цепочка
`if q.startswith(...)` / `if "..." in q`, первое совпадение выигрывает.
Значит ПОРЯДОК ВЕТОК = приоритет. Если общий шаблон стоит выше более
специфичного, специфичная ветка не выполнится НИКОГДА — а тест, который её
ждал, всё равно позеленеет, потому что общая ветка вернёт правдоподобные
данные. Ровно так это выстрелило в REP-ACL 02.08.2026: общий SELECT
перехватил author-scoped запрос, и тест прошёл бы при НЕРАБОТАЮЩЕМ фильтре
доступа. Ошибка молчит дважды: и в проде (фильтр снят), и в прогоне (зелено).

ЧТО ПРОВЕРЯЕТСЯ. Два источника, оба сломаны мутацией при сдаче:
  1. ПЕРЕХВАТ. Если шаблон ранней ветки — подстрока шаблона поздней, то любой
     запрос, подходящий поздней, сначала совпадёт с ранней. Поздняя мертва.
  2. ОХВАТ. Разбор обязан НАХОДИТЬ ветки. Ноль разобранных веток означает,
     что сломался сам разбор (переписали FakePool, сменили стиль условий),
     и «перехватов не найдено» тогда читается как «проверено», хотя проверено
     ничего. Отдельный порог на каждый метод, а не общий счётчик.

ГРАНИЦЫ, ЧЕСТНО.
  • Сравниваются ЛИТЕРАЛЫ условий. Ветку, чьё условие собрано из переменной
    или регулярки, разбор не увидит — такой стиль в FakePool не используется,
    и если появится, счётчик веток просядет и тест это заметит.
  • Проверяется ПОРЯДОК, а не СЕМАНТИКА: то, что ветка честно эмулирует
    PostgreSQL (org-scope, ACL, агрегаты), этим тестом не доказывается.
    Настоящий SQL — задача T36.
  • Подстрочное включение — достаточное условие перехвата, но не необходимое:
    два по-разному сформулированных шаблона могут пересекаться и без него.
"""

import ast
import pathlib

import pytest

CONFTEST = pathlib.Path(__file__).with_name("conftest.py")

# Ниже этого числа веток в методе разбор считается сломанным, а не «чисто».
# Значения взяты с запасом вниз от фактических на 06.08.2026
# (fetchval 4, fetch 20, fetchrow 28, execute 18).
MIN_BRANCHES = {"fetchval": 3, "fetch": 15, "fetchrow": 20, "execute": 12}


def _patterns_in(test_node):
    """Строковые шаблоны условия: q.startswith("X"[, "Y"]) и "X" in q."""
    found = []
    for node in ast.walk(test_node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append(("префикс", arg.value))
                elif isinstance(arg, ast.Tuple):
                    for el in arg.elts:
                        if isinstance(el, ast.Constant) and isinstance(el.value, str):
                            found.append(("префикс", el.value))
        if isinstance(node, ast.Compare) and any(
            isinstance(op, ast.In) for op in node.ops
        ):
            if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                found.append(("подстрока", node.left.value))
    return found


def _branches_by_method():
    """{имя метода FakePool: [(строка, вид, шаблон)] в порядке появления}."""
    tree = ast.parse(CONFTEST.read_text())
    out = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        if cls.name != "FakePool":
            continue
        for fn in cls.body:
            if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            pats = []
            for stmt in ast.walk(fn):
                if isinstance(stmt, ast.If):
                    for kind, lit in _patterns_in(stmt.test):
                        pats.append((stmt.lineno, kind, " ".join(lit.split())))
            if pats:
                out[fn.name] = sorted(set(pats))
    return out


BRANCHES = _branches_by_method()


def test_fakepool_parsed_at_all():
    """Источник 2: разбор нашёл ветки. Ноль = сломан разбор, а не «чисто»."""
    assert BRANCHES, (
        "В FakePool не найдено ни одной ветки разбора запроса. Либо класс "
        "переименован, либо условия пишутся иначе — тест ниже стал бы "
        "бессмысленно зелёным."
    )


@pytest.mark.parametrize("method,minimum", sorted(MIN_BRANCHES.items()))
def test_fakepool_branch_coverage(method, minimum):
    """Источник 2: у КАЖДОГО метода веток не меньше порога.

    Общий счётчик тут не годится: fetchrow может обрасти ветками, прикрыв
    просадку в execute, и охват «в среднем» скажет, что всё хорошо.
    """
    assert method in BRANCHES, f"FakePool.{method}: разбор не нашёл ни одной ветки"
    assert len(BRANCHES[method]) >= minimum, (
        f"FakePool.{method}: разобрано {len(BRANCHES[method])} веток при пороге "
        f"{minimum}. Либо ветки удалили, либо разбор перестал их видеть — "
        f"в обоих случаях проверка перехвата ослепла."
    )


def _branches(method):
    """Ветки метода как [(строка, [литералы условия])] в порядке появления."""
    out = {}
    for line, _kind, pat in BRANCHES[method]:
        out.setdefault(line, []).append(pat)
    return sorted(out.items())


@pytest.mark.parametrize("method", sorted(_branches_by_method()))
def test_fakepool_no_branch_interception(method):
    """Источник 1: ранняя ветка не покрывает позднюю.

    СРАВНИВАЕМ ВЕТКУ ЦЕЛИКОМ, А НЕ ЛИТЕРАЛЫ ПООДИНОЧКЕ. Ветка A перехватывает
    ветку B, если КАЖДЫЙ литерал условия A входит в какой-то литерал B — тогда
    A истинна всегда, когда истинна B. Литерал-в-литерал давал ложную тревогу
    на составных условиях: `startswith("INSERT INTO categories") and
    "RETURNING" in q` объявлял мёртвой более позднюю ветку с «RETURNING *»,
    хотя INSERT и UPDATE не совпадут никогда. Поймано 07.08.2026 на первой же
    настоящей правке FakePool — сторож сработал, но не на том.
    """
    br = _branches(method)
    problems = []
    for i, (line_a, lits_a) in enumerate(br):
        for line_b, lits_b in br[i + 1 :]:
            if set(lits_a) == set(lits_b):
                continue
            if all(any(a in b for b in lits_b) for a in lits_a):
                problems.append(
                    f"\n  строка {line_a} перехватывает строку {line_b}:"
                    f"\n    ранняя:  {' + '.join('«' + a + '»' for a in lits_a)}"
                    f"\n    поздняя: {' + '.join('«' + b + '»' for b in lits_b)}"
                )
    assert not problems, (
        f"FakePool.{method}: более общая ветка стоит выше специфичной, "
        f"специфичная не выполнится никогда:" + "".join(problems) + "\n\n"
        "  Починка: поднять специфичную ветку ВЫШЕ общей. Тест, который ждал "
        "специфичного поведения (обычно фильтр доступа), сейчас зелёный "
        "по случайности."
    )
