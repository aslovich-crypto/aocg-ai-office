# -*- coding: utf-8 -*-
"""T199 ①: вход в трекер — в CI красное, если строка заведена без решения владельца.

⚠️ ПОЧЕМУ ТЕСТ, А НЕ ТОЛЬКО ХУК. Хук pre-commit на трекере — предупреждение, не
блокировка (решение владельца 22.09.2026, Р3: блокировка учит обходить хук).
Значит единственное место, где нарушение делает красным, — CI. Этот файл — оно.
Сами приборы живут в `tests/tools` и pytest их не собирает, поэтому зовутся
отсюда подпроцессом, как их зовёт человек.
"""

import os
import subprocess
import sys

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ИНСТРУМЕНТЫ = os.path.join(КОРЕНЬ, "tests", "tools")


def _прогнать(прибор, *аргументы):
    return subprocess.run(
        [sys.executable, "-B", os.path.join(ИНСТРУМЕНТЫ, прибор), *аргументы],
        capture_output=True,
        text=True,
    )


def test_сторож_входа_проходит_самопроверку():
    р = _прогнать("tracker_entry_guard.py", "--selfcheck")
    assert р.returncode == 0, р.stdout + р.stderr


def test_в_трекере_нет_строк_мимо_решения_владельца():
    р = _прогнать("tracker_entry_guard.py")
    assert р.returncode == 0, р.stdout + р.stderr
    # Вторая половина: прибор ОТРАБОТАЛ, а не промолчал — число задач напечатано.
    assert "строк-задач в файле" in р.stdout


def test_сторож_штампов_знает_оба_вида():
    р = _прогнать("audit_stamps.py", "--selfcheck")
    assert р.returncode == 0, р.stdout + р.stderr
    assert "✓ здоровый штамп решения проходит молча" in р.stdout


def test_форма_всех_штампов_в_трекере_верна():
    р = _прогнать("audit_stamps.py")
    assert р.returncode == 0, р.stdout + р.stderr
