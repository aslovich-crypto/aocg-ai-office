# -*- coding: utf-8 -*-
"""Витрина трекера: docs/TASKS.md → HTML для чтения глазами.

⚠️ ЭТО ПРЕДСТАВЛЕНИЕ, А НЕ КОПИЯ. Страница собирается в /tmp при каждом
вызове и в git НЕ КЛАДЁТСЯ. Отставание становится невозможным ПО УСТРОЙСТВУ,
а не маловероятным: читать нечего, кроме файла.

⚠️ ЗАЧЕМ ТАК, А НЕ ХУКОМ. Прежняя витрина (артефакт на claude.ai) собиралась
руками и отставала на два дня: файл правится по семь раз за день. Ставить
сборку в pre-commit значило бы класть готовый HTML в git — и вернуть вторую
копию, ровно то, от чего уходим. Плюс diff перестал бы читаться: правил одну
строку — в коммите две тысячи.

⚠️ РАЗБОР НЕ ПЕРЕПИСЫВАЕТСЯ. `строки_задач` (tracker_guard) и `ячейки`
(md_table) уже под тестами и работают — граница поставлена владельцем.
Здесь только сборка и показ.
"""

import html
import importlib.util
import pathlib
import sys

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
ТРЕКЕР = КОРЕНЬ / "docs/TASKS.md"
ВЫХОД = pathlib.Path("/tmp/aocg-tracker.html")


def _модуль(имя):
    спец = importlib.util.spec_from_file_location(
        имя, pathlib.Path(__file__).with_name(f"{имя}.py")
    )
    м = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(м)
    return м


tracker_guard = _модуль("tracker_guard")
md_table = _модуль("md_table")

ЦВЕТ_ПРИОРИТЕТА = {"🔴": "#A4161A", "🟡": "#B45309", "🟢": "#166534"}
ФОН_СТАТУСА = {
    "✅": ("#F0FDF4", "#166534"),
    "⬜": ("#F8FAFC", "#475569"),
    "🔵": ("#EFF6FF", "#1D4ED8"),
    "⏸": ("#FFFBEB", "#B45309"),
    "👀": ("#FAF5FF", "#7E22CE"),
}


def собрать(текст):
    """(разделы, задачи). Раздел — (заголовок, уровень, [задачи])."""
    задачи = tracker_guard.строки_задач(текст)
    по_строке = {н: (ид, пр, ст, план, факт) for н, ид, пр, ст, план, факт in задачи}

    разделы, текущий = [], None
    for н, с in enumerate(текст.split("\n"), 1):
        if с.startswith("## ") or с.startswith("### "):
            уровень = 2 if с.startswith("## ") else 3
            текущий = (с.lstrip("# ").strip(), уровень, [])
            разделы.append(текущий)
            continue
        if н in по_строке:
            ид, пр, ст, план, факт = по_строке[н]
            # примечание — последняя ячейка строки; разбор чужой, не наш
            я = md_table.ячейки(с)
            название = я[1].strip() if len(я) > 1 else ""
            примечание = я[6].strip() if len(я) > 6 else ""
            запись = (ид, название, пр, ст, план, факт, примечание, н)
            (текущий[2] if текущий else разделы.setdefault(0, ("", 2, []))[2]).append(
                запись
            )
    return разделы, задачи


def в_html(разделы, всего):
    ч = []
    for заголовок, уровень, строки in разделы:
        if not строки:
            continue
        ч.append(f"<h{уровень}>{html.escape(заголовок)}</h{уровень}>")
        for ид, название, пр, ст, план, факт, прим, н in строки:
            цвет = ЦВЕТ_ПРИОРИТЕТА.get(пр, "#94A3B8")
            фон, текст_ст = ФОН_СТАТУСА.get(ст, ("#F8FAFC", "#475569"))
            ч.append(
                f'<article style="border-left:4px solid {цвет}">'
                f'<div class="шапка">'
                f'<span class="ид">{html.escape(ид)}</span>'
                f'<span class="имя">{html.escape(название)}</span>'
                f'<span class="ст" style="background:{фон};color:{текст_ст}">{html.escape(ст)}</span>'
                f"</div>"
                f'<div class="мета">план {html.escape(план or "—")} · факт {html.escape(факт or "—")}'
                f' · <span class="строка">TASKS.md:{н}</span></div>'
                + (
                    f'<details><summary>примечание</summary><div class="прим">{html.escape(прим)}</div></details>'
                    if прим
                    else ""
                )
                + "</article>"
            )
    return "\n".join(ч)


СТИЛЬ = """
:root{--фон:#FFFFFF;--текст:#111318;--серый:#64748B;--рамка:#E2E8F0}
@media (prefers-color-scheme:dark){:root{--фон:#0F1115;--текст:#E8EAED;--серый:#94A3B8;--рамка:#232833}}
*{box-sizing:border-box}
body{margin:0;padding:24px 20px 64px;background:var(--фон);color:var(--текст);
     font:400 15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     max-width:900px;margin-inline:auto}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:17px;margin:32px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--рамка)}
h3{font-size:14px;margin:20px 0 8px;color:var(--серый);text-transform:uppercase;letter-spacing:.04em}
.свод{color:var(--серый);font-size:13px;margin-bottom:8px}
article{padding:10px 12px;margin:6px 0;background:var(--фон);border:1px solid var(--рамка);border-radius:6px}
.шапка{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.ид{font-weight:700;font-variant-numeric:tabular-nums;flex-shrink:0}
.имя{flex:1;min-width:200px}
.ст{font-size:12px;padding:1px 7px;border-radius:999px;flex-shrink:0}
.мета{font-size:12px;color:var(--серый);margin-top:3px}
.строка{font-family:ui-monospace,SFMono-Regular,monospace}
details{margin-top:6px}
summary{font-size:12px;color:var(--серый);cursor:pointer}
.прим{font-size:13px;color:var(--текст);margin-top:6px;padding-left:10px;
      border-left:2px solid var(--рамка);white-space:pre-wrap;overflow-wrap:anywhere}
"""


def main():
    if not ТРЕКЕР.exists():
        sys.exit(f"⚠️ СБОРКА НЕ ВЫПОЛНЕНА: нет {ТРЕКЕР}")
    текст = ТРЕКЕР.read_text(encoding="utf-8")
    разделы, задачи = собрать(текст)

    # ⚠️ ПУСТАЯ ВИТРИНА БЕЗ ОШИБКИ — ТА ЖЕ ЛОЖЬ, ЧТО МОЛЧАЩИЙ ПРОПУСК (T87).
    # Разбор мог сломаться, формат таблиц — смениться, файл — обрезаться.
    # Красивая пустая страница выглядит исправной, и это худший исход.
    if not задачи:
        sys.exit(
            "⚠️ СБОРКА НЕ ВЫПОЛНЕНА: разобрано 0 задач из docs/TASKS.md.\n"
            "   Либо сменился формат таблиц, либо файл повреждён.\n"
            "   Пустая витрина хуже отсутствующей — не собираю."
        )

    ВЫХОД.write_text(
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>Трекер AOCG AI Офис</title><style>{СТИЛЬ}</style></head><body>"
        f"<h1>Трекер AOCG AI Офис</h1>"
        f'<div class="свод">задач {len(задачи)} · собрано из docs/TASKS.md · '
        f"представление, а не копия — в git не хранится</div>"
        + в_html(разделы, len(задачи))
        + "</body></html>",
        encoding="utf-8",
    )
    print(f"✓ витрина собрана: {ВЫХОД}")
    print(
        f"  задач {len(задачи)} · разделов с задачами "
        f"{sum(1 for _, _, с in разделы if с)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
