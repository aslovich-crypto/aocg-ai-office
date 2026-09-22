# -*- coding: utf-8 -*-
"""T199 ③: очередь — в CI красное на двух 🔵, на отступлении без записи и на битой
записи журнала отступлений. Плюс договор хука начала сессии: JSON с двумя каналами.

⚠️ МЕТКА ПРОГОНА ХУКА ПИШЕТСЯ ВО ВРЕМЕННЫЙ ФАЙЛ (AOCG_QUEUE_MARK). Настоящая метка
— доказательство замера О7 «хук сработал в VS Code»; строка от теста в ней
была бы неотличима от настоящего запуска хука.
"""

import json
import os
import subprocess
import sys

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ПРИБОР = os.path.join(КОРЕНЬ, "tests", "tools", "tracker_queue.py")


def _прогнать(*аргументы, stdin="", метка=None):
    окружение = dict(os.environ)
    if метка:
        окружение["AOCG_QUEUE_MARK"] = метка
    return subprocess.run(
        [sys.executable, "-B", ПРИБОР, *аргументы],
        capture_output=True,
        text=True,
        input=stdin,
        env=окружение,
    )


def test_сторож_очереди_проходит_самопроверку():
    р = _прогнать("--selfcheck")
    assert р.returncode == 0, р.stdout + р.stderr


def test_очередь_в_трекере_без_нарушений():
    р = _прогнать()
    assert р.returncode == 0, р.stdout + р.stderr
    # Вторая половина: прибор ОТРАБОТАЛ — разобрал блок и назвал следующую.
    assert "следующая" in р.stdout and "пунктов в блоке" in р.stdout
    # T200 ②: счётчик короткой дорожки напечатан — прибор прочёл метки, на которых
    # стоит правило ⑬.
    assert "короткая дорожка (🚀)" in р.stdout


def test_хук_отдаёт_оба_канала_и_пишет_метку(tmp_path):
    метка = tmp_path / "mark.log"
    р = _прогнать("--hook", stdin='{"source": "pytest"}', метка=str(метка))
    assert р.returncode == 0, р.stderr
    данные = json.loads(р.stdout)
    assert данные["systemMessage"].startswith("▶ следующая:")
    assert данные["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "ПРАВИЛО ОЧЕРЕДИ" in данные["hookSpecificOutput"]["additionalContext"]
    # T200 ⑤, В6: строка хука — ПЕРВОЙ строкой канала модели, слово в слово.
    контекст = данные["hookSpecificOutput"]["additionalContext"]
    assert контекст.split("\n")[0] == данные["systemMessage"]
    assert "короткая: открыто" in данные["systemMessage"]
    # Метка ушла во временный файл, с источником — а не в настоящую метку.
    assert "· pytest ·" in метка.read_text(encoding="utf-8")


def test_хук_на_сломанном_трекере_говорит_в_оба_канала(tmp_path):
    трекер = tmp_path / "TASKS.md"
    трекер.write_text("# трекер без блока порядка\n", encoding="utf-8")
    р = _прогнать("--hook", str(трекер), метка=str(tmp_path / "m.log"))
    assert р.returncode == 0
    данные = json.loads(р.stdout)
    assert данные["systemMessage"].startswith("⚠️ ОЧЕРЕДЬ НЕ ПРОЧИТАНА")
    контекст = данные["hookSpecificOutput"]["additionalContext"]
    assert (
        контекст.startswith("⚠️ ОЧЕРЕДЬ НЕ ПРОЧИТАНА") and "ПЕРВОЙ строкой" in контекст
    )
