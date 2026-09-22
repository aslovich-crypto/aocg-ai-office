# -*- coding: utf-8 -*-
"""T200 ③: витрина трекера — в CI красное, если она разошлась с файлом.

⚠️ ПОЧЕМУ ТЕСТ. До 22.09.2026 сторож витрины шёл только в pre-commit и с «|| true»,
то есть предупреждением, а в CI не шёл вовсе. Решение владельца 22.09.2026 (В4):
витрина наравне со сторожами входа и очереди — сломалась, значит красное.

⚠️ ВИТРИНА ПИШЕТСЯ ВО ВРЕМЕННЫЙ КАТАЛОГ (AOCG_TRACKER_HTML): тест не вправе
подменять страницу в /tmp, которую открывает владелец.
"""

import os
import subprocess
import sys

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
СТОРОЖ = os.path.join(КОРЕНЬ, "tests", "tools", "tracker_html_guard.py")


def test_витрина_отражает_трекер(tmp_path):
    витрина = tmp_path / "витрина.html"
    р = subprocess.run(
        [sys.executable, "-B", СТОРОЖ],
        capture_output=True,
        text=True,
        env=dict(os.environ, AOCG_TRACKER_HTML=str(витрина)),
    )
    assert р.returncode == 0, р.stdout + р.stderr
    assert "ИТОГ: витрина отражает файл целиком" in р.stdout
    # Вторая половина: сторож ОТРАБОТАЛ по новым разделам, а не промолчал,
    # и страница легла туда, куда велели.
    for что in ("ОТСТУПЛЕНИЯ", "ЧЕРНОВИКИ", "УЛИКИ", "ДОРОЖКИ", "СИГНАЛ РАЗБОРА"):
        assert что in р.stdout, что
    assert витрина.exists()
