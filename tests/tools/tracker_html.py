# -*- coding: utf-8 -*-
"""Витрина трекера: docs/TASKS.md → HTML для чтения глазами.

⚠️ ЭТО ПРЕДСТАВЛЕНИЕ, А НЕ КОПИЯ. Страница собирается в /tmp при каждом
вызове и в git НЕ КЛАДЁТСЯ. Отставание становится невозможным ПО УСТРОЙСТВУ,
а не маловероятным: читать нечего, кроме файла.

⚠️ ЗАЧЕМ ТАК, А НЕ ХУКОМ. Прежняя витрина (артефакт на claude.ai) собиралась
руками и отстала на два дня и 18 задач: 228 против 246. Ставить сборку
в pre-commit значило бы класть готовый HTML в git — и вернуть вторую копию,
ровно то, от чего уходим.

⚠️ РАЗБОР НЕ ПЕРЕПИСЫВАЕТСЯ. `строки_задач` (tracker_guard) и `ячейки`
(md_table) уже под тестами и работают — граница поставлена владельцем.
Здесь только сборка и показ.

⚠️ ПОЧЕМУ СТРОКИ ПЕЧАТАЕТ СБОРЩИК, А НЕ СКРИПТ В БРАУЗЕРЕ. Прежняя витрина
держала задачи одним куском JSON и рисовала строки на клиенте — в самом
файле не было НИ ОДНОГО <article>. Повтори её дословно — сторож насчитал бы
ноль вместо 246 и покраснел. Печать строк сборщиком оставляет контракт
со сторожем целым, а Ctrl+F и печать страницы берут ВСЕ задачи.
"""

import collections
import html
import importlib.util
import pathlib
import re
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

КЛАСС_ПРИОРИТЕТА = {"🔴": "crit", "🟡": "mid", "🟢": "low"}
КЛАСС_СТАТУСА = {"✅": "done", "👀": "look", "🔵": "look", "⏸": "hold"}


def разметка_строки(текст):
    """`код`, **жирный**, [[ссылка]] — как в прежней витрине.

    ⚠️ ПРИМЕНЯЕТСЯ И К ИМЕНАМ ЗАДАЧ. В прежней витрине имена шли сырыми:
    LEGAL-005 читалась как «**[ДО-КЛИЕНТА]** … `aocgai.ru` …» вместе
    со звёздочками и обратными кавычками. Дефект оригинала не возвращаем.

    ⚠️ ПОРЯДОК ЗДЕСЬ — НЕ ВКУСОВЩИНА, ОН ИЗМЕРЕН. Кодовые вставки прячутся
    заглушкой ДО разбора жирного, и обе беды снимаются разом:
    ① `**` внутри кода (`Write(**/.env)`) больше не смыкается с парой
      снаружи вставки — раньше выходил перехлёст <code>…<strong>…</code>,
      три штуки на странице;
    ② жирное, ВНУТРИ которого лежит код («**… фронт `fe01d98` …**»),
      остаётся целым — а такого в трекере большинство. Резать строку
      по коду нельзя: пары ** разрываются, замер дал 556 сырых знаков
      вместо 26.
    """
    ч = html.escape(текст)
    коды = []

    def _спрятать(м):
        коды.append(м.group(1))
        return f"\x00{len(коды) - 1}\x00"

    ч = re.sub(r"`([^`]+)`", _спрятать, ч)
    # ⚠️ (.+?), А НЕ ([^*]+): жирное сплошь и рядом содержит одиночную
    # звёздочку — CORS_ORIGINS → ["*"], глоб *.py. Класс символов на ней
    # ломался, и оба знака ** оставались видны глазами.
    ч = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", ч, flags=re.S)
    ч = re.sub(r"\[\[([^\]]+)\]\]", r'<span class="ref">\1</span>', ч)
    # ⚠️ Зачёркивание нашлось ЗАМЕРОМ ПО СТРАНИЦЕ, а не по коду: восемь
    # знаков ~~ торчали в именах четырёх задач (MOB-1, 28, 30, MOB-4).
    # Сторож их не проверял — набор сырых знаков дополнен в T101.
    ч = re.sub(r"~~(.+?)~~", r"<s>\1</s>", ч, flags=re.S)
    return re.sub(
        r"\x00(\d+)\x00", lambda м: f"<code>{коды[int(м.group(1))]}</code>", ч
    )


def разметка_примечания(текст):
    """═══ делит разбор на абзацы, ⚠️-абзац идёт вишнёвой плашкой."""
    куски = [к.strip() for к in текст.split("═══") if к.strip()]
    абзацы = []
    for к in куски:
        предупреждение = к.startswith("⚠️") or к.startswith("**⚠️")
        кл = ' class="warn"' if предупреждение else ""
        абзацы.append(f"<p{кл}>{разметка_строки(к)}</p>")
    return "".join(абзацы) or "<p>—</p>"


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


def порядок_выполнения(текст):
    """Разбирает блок «▶ ПОРЯДОК ВЫПОЛНЕНИЯ»: [(номер, [ид...], текст), ...].

    ⚠️ БЛОК — ЕДИНСТВЕННЫЙ ИСТОЧНИК ПОРЯДКА (решение владельца 31.08.2026).
    Витрина обязана печатать его наверху, а номера вести НА КАРТОЧКИ, иначе
    порядок жил бы в файле и не жил в том, что читают глазами."""
    м = re.search(r"^## ▶ ПОРЯДОК ВЫПОЛНЕНИЯ\s*$([\s\S]*?)(?=^## )", текст, re.M)
    if not м:
        return []
    пункты = []
    for строка in м.group(1).split("\n"):
        с = строка.strip()
        мн = re.match(r"^(\d+)\.\s+(.*)$", с)
        if not мн:
            continue
        пункты.append((мн.group(1), re.findall(r"\[\[([^\]]+)\]\]", с), мн.group(2)))
    return пункты


def блок_порядка(пункты):
    """HTML-навигация порядка. Каждый [[ид]] — ссылка на карточку #ид.

    ⚠️ ЭТО НЕ КАРТОЧКИ И НЕ <article> — сторож НАЙДЕНО/НАПЕЧАТАНО их не
    считает, фильтр (T99) их не трогает: блок стоит над панелью и виден
    при любом фильтре, как оглавление."""
    if not пункты:
        return ""
    строки = []
    for номер, иды, текст in пункты:
        х = html.escape(текст)
        for ид in иды:
            бе = html.escape(ид)
            х = х.replace(
                f"[[{бе}]]", f'<a class="oref" href="#{бе}">{бе}</a>'
            )
        строки.append(f"<li>{х}</li>")
    return (
        '<nav class="order" aria-label="Порядок выполнения">'
        "<h2>▶ Порядок выполнения</h2><ol>" + "".join(строки) + "</ol></nav>"
    )


def строка_задачи(запись):
    ид, название, пр, ст, план, факт, прим, н = запись
    полоска = КЛАСС_ПРИОРИТЕТА.get(пр, "")
    пилюля = КЛАСС_СТАТУСА.get(ст, "")
    план_ф = план if план and план != "—" else ""
    факт_ф = факт if факт and факт != "—" else ""
    сроки = " · ".join(
        ч
        for ч in (
            f"план {план_ф}" if план_ф else "",
            f"факт {факт_ф}" if факт_ф else "",
        )
        if ч
    )
    # ⚠️ ПОЛНЫЕ СРОКИ ЖИВУТ В ПРИМЕЧАНИИ, А НЕ В СТРОКЕ. Замер 29.08.2026:
    # медиана «план · факт» — 0 знаков, но у четырёх задач она до 261 (№30),
    # 172 (MOB-4), 150 (№28). В строке такая ячейка распирала сетку, имя
    # схлопывалось в один слог по вертикали, а хвост уезжал за край экрана.
    # Теперь в строке усечённая ячейка с подсказкой, а полностью — здесь:
    # ничего не потеряно, Ctrl+F и поиск по разбору по-прежнему находят.
    шапка_сроков = f'<p class="сроки">{html.escape(сроки)}</p>' if сроки else ""
    # ⚠️ ПРИЗНАКИ ВИСЯТ НА САМОЙ КАРТОЧКЕ, И ЭТО НЕ УКРАШЕНИЕ. Фильтр обязан
    # ПРЯТАТЬ уже напечатанные <article>, а не рисовать их скриптом из JSON:
    # иначе сторож насчитает ноль вместо 249. Ровно эту ловушку содержала
    # прежняя витрина — см. T98.
    # ⚠️ Ссылка на строку файла — добавка к прежнему виду, оставлена
    # по решению владельца: видно, куда править.
    return (
        # ⚠️ id — ЯКОРЬ для блока порядка и любых ссылок на карточку.
        f'<article class="task" id="{html.escape(ид, quote=True)}" '
        f'data-pr="{html.escape(пр, quote=True)}" '
        f'data-st="{html.escape(ст, quote=True)}">'
        '<details><summary class="row">'
        f'<span class="stripe {полоска}"></span>'
        f'<span class="tid">{html.escape(ид)}</span>'
        f'<span class="tname">{разметка_строки(название)}</span>'
        '<span class="meta">'
        f'<span class="pill {пилюля}">{html.escape(ст)}</span>'
        + (
            f'<span class="fact" title="{html.escape(сроки, quote=True)}">'
            f"{html.escape(сроки)}</span>"
            if сроки
            else ""
        )
        + f'<span class="line">TASKS.md:{н}</span>'
        "</span></summary>"
        f'<div class="note">{шапка_сроков}{разметка_примечания(прим)}</div>'
        "</details></article>"
    )


def в_html(разделы):
    ч = []
    for заголовок, уровень, строки in разделы:
        if not строки:
            continue
        класс = "blk" if уровень == 3 else "sec-h"
        # ⚠️ <section> нужен, чтобы при фильтре пропадал и ЗАГОЛОВОК тоже.
        # Без него остаются висеть заголовки блоков без единой задачи под ними.
        ч.append(f'<section class="sec" data-b="{html.escape(заголовок, quote=True)}">')
        ч.append(
            f'<h{уровень} class="{класс}">{разметка_строки(заголовок)}</h{уровень}>'
        )
        ч.append('<div class="list">')
        ч.extend(строка_задачи(з) for з in строки)
        ч.append("</div></section>")
    return "\n".join(ч)


def сводка(задачи):
    """Шесть чисел, как в прежней витрине. Считаются, а не проставляются."""
    ст = collections.Counter(з[3] for з in задачи)
    пр = collections.Counter(з[2] for з in задачи)
    с_фактом = sum(1 for з in задачи if з[3] == "✅" and з[5] and з[5] != "—")
    карточки = (
        ("", ст.get("⬜", 0), "к работе"),
        ("crit", пр.get("🔴", 0), "🔴 критично"),
        ("mid", пр.get("🟡", 0), "🟡 средне"),
        ("low", пр.get("🟢", 0), "🟢 низко"),
        ("", ст.get("✅", 0), "✅ готово"),
        ("", с_фактом, "из них с фактом"),
    )
    тела = "".join(
        f'<div class="stat {к}"><b>{ч}</b><span>{п}</span></div>'
        for к, ч, п in карточки
    )
    return f'<div class="stats">{тела}</div>', пр


def панель(разделы, пр, всего):
    """Поиск, кнопки приоритета и статуса, список блоков, счётчик."""
    приоритеты = "".join(
        f'<button class="chip" aria-pressed="false" data-v="{з}">{з} {пр.get(з, 0)}</button>'
        for з in ("🔴", "🟡", "🟢")
    )
    статусы = "".join(
        f'<button class="chip" aria-pressed="false" data-v="{з}">{з} {п}</button>'
        for з, п in (
            # ⚠️ ПОДПИСИ ДОСЛОВНО ИЗ ЛЕГЕНДЫ docs/TASKS.md, А НЕ ИЗ ГОЛОВЫ.
            # Было расхождение в ЧЕТЫРЁХ из пяти: «проверка» вместо
            # «наблюдаем», «блок» вместо «ждёт/заблокировано», «открыто»
            # вместо «к работе», «закрыто» вместо «готово». Своё слово
            # рядом с легендой — это второй словарь: читатель видит
            # «проверка» и думает про приёмку, а статус значит «следим,
            # чинить сейчас дороже, чем терпеть».
            ("⬜", "к работе"),
            ("✅", "готово"),
            ("👀", "наблюдаем"),
            ("🔵", "в работе"),
            ("⏸", "ждёт/заблокировано"),
        )
    )
    блоки = "".join(
        f'<option value="{html.escape(з, quote=True)}">{html.escape(з)}</option>'
        for з, _, стр in разделы
        if стр
    )
    return (
        '<div class="bar">'
        '<input type="search" id="q" autocomplete="off" '
        'placeholder="Поиск по номеру, названию и разбору" aria-label="Поиск">'
        f'<div class="chips" id="pr" role="group" aria-label="Приоритет">{приоритеты}</div>'
        f'<div class="chips" id="st" role="group" aria-label="Статус">{статусы}</div>'
        f'<select id="blk" aria-label="Блок"><option value="">все блоки</option>{блоки}</select>'
        f'<span class="count" id="cnt">{всего} из {всего}</span>'
        "</div>"
        '<p class="empty" id="empty" hidden>Ничего не нашлось. '
        "Снимите фильтры или измените запрос.</p>"
    )


# ⚠️ ШРИФТЫ ТЯНУТСЯ С fonts.googleapis.com, А ВИТРИНА ОТКРЫВАЕТСЯ КАК file://.
# Без сети запрос не пройдёт, и страница молча съедет на запасной стек —
# решение владельца: пережить, а не вшивать шрифты в файл (+400 КБ в /tmp).
# Поэтому у каждого начертания честный запасной стек, а не голое имя.
ШРИФТЫ = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600"
    '&family=IBM+Plex+Serif:wght@500;600&display=swap">'
)

СТИЛЬ = """
:root{
  --ground:#F5F6F9; --surface:#FFFFFF; --raise:#FAFBFC;
  --ink:#14181F; --muted:#5C6674; --faint:#8A94A3;
  --line:#E1E5EC; --line-soft:#EDF0F4;
  --cherry:#A4161A; --cherry-soft:#F6E9EA;
  --crit:#A4161A; --mid:#8A5510; --low:#2E6B4F;
  --shadow:0 1px 2px rgba(20,24,31,.05), 0 8px 24px -16px rgba(20,24,31,.18);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ground:#12151A; --surface:#191D24; --raise:#1E232B;
    --ink:#E7EAF0; --muted:#98A2B1; --faint:#6E7887;
    --line:#2A303A; --line-soft:#232830;
    --cherry:#E4585F; --cherry-soft:#2A1618;
    --crit:#E4585F; --mid:#D9A055; --low:#6FC098;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 30px -18px rgba(0,0,0,.8);
  }
}
:root[data-theme="dark"]{
  --ground:#12151A; --surface:#191D24; --raise:#1E232B;
  --ink:#E7EAF0; --muted:#98A2B1; --faint:#6E7887;
  --line:#2A303A; --line-soft:#232830;
  --cherry:#E4585F; --cherry-soft:#2A1618;
  --crit:#E4585F; --mid:#D9A055; --low:#6FC098;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 30px -18px rgba(0,0,0,.8);
}
*{box-sizing:border-box}
body{
  /* ⚠️ overflow-x:hidden УБРАН СОЗНАТЕЛЬНО. Он прятал не беду, а её
     признак: содержимое всё равно уезжало за край, только молча
     обрезалось. И он ослеплял проверку размещения (T102) — боковую
     прокрутку стало бы нечем поймать. Причина чинится в сетке. */
  margin:0 auto; max-width:1140px; padding:0 20px 80px;
  background:var(--ground); color:var(--ink);
  font-family:"IBM Plex Sans","Helvetica Neue","Segoe UI",Roboto,Arial,sans-serif;
  font-size:15px; line-height:1.55; -webkit-font-smoothing:antialiased;
}
header{padding:44px 0 26px; border-bottom:1px solid var(--line)}
h1{
  font-family:"IBM Plex Serif",Georgia,"Times New Roman",serif; font-weight:600;
  font-size:clamp(26px,4vw,36px); letter-spacing:-.015em; margin:0 0 6px;
  text-wrap:balance;
}
.sub{color:var(--muted); margin:0; max-width:62ch}
.src{
  font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:12px; color:var(--faint); margin-top:14px;
  display:flex; gap:14px; flex-wrap:wrap;
}
.sec{
  font-family:"IBM Plex Sans","Helvetica Neue",Arial,sans-serif;
  font-size:15px; font-weight:600; margin:38px 0 10px;
  padding-bottom:7px; border-bottom:1px solid var(--line);
}
.blk{
  font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace; font-weight:400;
  font-size:11px; color:var(--faint); letter-spacing:.02em; margin:26px 0 8px;
}
.stats{display:flex; flex-wrap:wrap; gap:10px; margin:22px 0 0}
.stat{
  background:var(--surface); border:1px solid var(--line); border-radius:3px;
  padding:10px 14px; min-width:104px; box-shadow:var(--shadow);
}
.stat b{
  display:block; font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;
  font-size:22px; font-weight:600; font-variant-numeric:tabular-nums; line-height:1.2;
}
.stat span{font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.07em}
.stat.crit b{color:var(--crit)}
.stat.mid b{color:var(--mid)}
.stat.low b{color:var(--low)}
.bar{
  position:sticky; top:0; z-index:5; background:var(--ground);
  padding:16px 0 14px; border-bottom:1px solid var(--line);
  display:flex; gap:10px; flex-wrap:wrap; align-items:center;
}
input[type=search],select{
  font:inherit; font-size:14px; color:var(--ink); background:var(--surface);
  border:1px solid var(--line); border-radius:3px; padding:8px 11px;
}
input[type=search]{flex:1 1 240px; min-width:180px}
input[type=search]:focus-visible,select:focus-visible,.chip:focus-visible{
  outline:2px solid var(--cherry); outline-offset:1px;
}
.chips{display:flex; gap:6px; flex-wrap:wrap}
.chip{
  font:inherit; font-size:13px; cursor:pointer; background:var(--surface);
  border:1px solid var(--line); border-radius:3px; padding:7px 11px; color:var(--muted);
}
.chip[aria-pressed="true"]{background:var(--ink); color:var(--ground); border-color:var(--ink)}
.count{
  font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace; font-size:12px;
  color:var(--faint); margin-left:auto; font-variant-numeric:tabular-nums;
}
.empty{padding:48px 0; color:var(--muted); text-align:center}
.list{margin:0 0 4px}
.task{
  background:var(--surface); border:1px solid var(--line); border-top:none;
}
.list .task:first-child{border-top:1px solid var(--line)}
details>summary{list-style:none}
details>summary::-webkit-details-marker{display:none}
.row{
  /* ⚠️ ПОСЛЕДНЯЯ ДОРОЖКА ОГРАНИЧЕНА ЧИСЛОМ, А НЕ auto. max-width
     на самой ячейке НЕ сжимает дорожку сетки: под auto она требует
     ширину по содержимому, а у сроков это до 261 знака в одну
     строку. Дорожка забирала всё, имени доставался его минимум
     220px, и справа зияло пустое поле в пол-экрана. */
  display:grid; grid-template-columns:14px 88px minmax(220px,1fr) minmax(0,340px);
  gap:0 14px; align-items:baseline; width:100%; text-align:left; cursor:pointer;
  padding:11px 14px 11px 0;
}
.row:hover{background:var(--raise)}
.row:focus-visible{outline:2px solid var(--cherry); outline-offset:-2px}
.stripe{align-self:stretch; width:4px}
.stripe.crit{background:var(--crit)}
.stripe.mid{background:var(--mid)}
.stripe.low{background:var(--low)}
.tid{
  font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace; font-size:12.5px;
  font-weight:500; color:var(--muted); font-variant-numeric:tabular-nums;
}
/* ⚠️ break-word, А НЕ anywhere. Разница не косметическая: anywhere
   МЕНЯЕТ минимальную ширину элемента до одного знака, и колонка имени
   схлопнулась в букву — задача №28 встала столбиком по вертикали.
   break-word переносит длинное слово, но минимальную ширину не трогает. */
.tname{font-size:14.5px; min-width:0; overflow-wrap:break-word}
.tname s{color:var(--faint)}
.tname code,.tname strong{font-size:inherit}
/* ⚠️ max-width — НЕ ВКУСОВЩИНА: без него ячейка сроков на 261 знак
   съедала колонку имени и выносила хвост за край страницы. */
.meta{
  display:flex; gap:8px; align-items:center; white-space:nowrap;
  min-width:0; max-width:100%; justify-self:end;
}
.meta>.pill,.meta>.line{flex-shrink:0}
.pill{
  font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace; font-size:11px;
  letter-spacing:.02em; border:1px solid var(--line); border-radius:2px;
  padding:2px 7px; color:var(--muted);
}
.pill.done{color:var(--low); border-color:color-mix(in srgb,var(--low) 40%,transparent)}
.pill.look{color:var(--cherry); border-color:color-mix(in srgb,var(--cherry) 40%,transparent)}
.pill.hold{color:var(--mid); border-color:color-mix(in srgb,var(--mid) 40%,transparent)}
.fact,.line{
  font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace; font-size:11.5px;
  color:var(--faint); font-variant-numeric:tabular-nums;
}
.fact{min-width:0; overflow:hidden; text-overflow:ellipsis}
.сроки{
  font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace; font-size:12px;
  color:var(--muted); margin:0 0 12px; padding-bottom:10px;
  border-bottom:1px solid var(--line-soft); overflow-wrap:anywhere;
}
.line{color:color-mix(in srgb,var(--faint) 70%,transparent)}
.note{
  background:var(--raise); border-top:1px solid var(--line-soft);
  padding:16px 20px 20px 32px; font-size:14px; color:var(--ink);
}
.note p{margin:0 0 11px; max-width:74ch; overflow-wrap:anywhere}
.note p:last-child{margin-bottom:0}
.note strong{font-weight:600}
code{
  font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace; font-size:12.5px;
  background:var(--surface); border:1px solid var(--line-soft);
  border-radius:2px; padding:1px 4px;
}
.note .warn{
  border-left:2px solid var(--cherry); background:var(--cherry-soft);
  margin-left:-14px; padding:8px 10px 8px 12px;
}
.order{
  background:var(--surface); border:1px solid var(--line); border-radius:10px;
  box-shadow:var(--shadow); margin:14px 0 4px; padding:12px 18px 14px;
}
.order h2{font:600 13px/1.2 "IBM Plex Sans",sans-serif; color:var(--cherry);
  letter-spacing:.06em; text-transform:uppercase; margin:0 0 8px}
.order ol{margin:0; padding-left:22px; font:400 13.5px/1.75 "IBM Plex Sans",sans-serif}
.order .oref{
  font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace; font-size:12.5px;
  color:var(--cherry); text-decoration:none;
  border-bottom:1px dotted color-mix(in srgb,var(--cherry) 50%,transparent);
}
.ref{
  font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace; font-size:12px;
  color:var(--cherry);
  border-bottom:1px dotted color-mix(in srgb,var(--cherry) 50%,transparent);
}
@media (max-width:720px){
  .row{grid-template-columns:14px 1fr; gap:0 10px}
  .tid,.meta{grid-column:2}
  .meta{margin-top:4px; white-space:normal; flex-wrap:wrap}
  .note{padding-left:20px}
  .note .warn{margin-left:-8px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


# ⚠️ СКРИПТ ТОЛЬКО ПРЯЧЕТ. Все 249 <article> уже напечатаны сборщиком и
# лежат в файле — фильтр переключает им атрибут hidden, и ничего не рисует.
# Отсюда три следствия: сторож считает карточки в ФАЙЛЕ и остаётся зелёным
# при любом фильтре; Ctrl+F браузера и печать берут все задачи; страница
# читается и со сломанным скриптом.
СКРИПТ = """
(function(){
  const карточки=[...document.querySelectorAll('article.task')].map(э=>({
    э:э, т:э.textContent.toLowerCase(), пр:э.dataset.pr, ст:э.dataset.st,
    б:э.closest('.sec').dataset.b}));
  const секции=[...document.querySelectorAll('section.sec')];
  const q=document.getElementById('q'), blk=document.getElementById('blk');
  const cnt=document.getElementById('cnt'), пусто=document.getElementById('empty');
  const выбор={pr:new Set(), st:new Set()};
  function рисуй(){
    const текст=q.value.trim().toLowerCase(), б=blk.value;
    let видно=0;
    for(const к of карточки){
      const ок=(!выбор.pr.size||выбор.pr.has(к.пр))
             &&(!выбор.st.size||выбор.st.has(к.ст))
             &&(!б||к.б===б)
             &&(!текст||к.т.includes(текст));
      к.э.hidden=!ок; if(ок) видно++;
    }
    for(const с of секции) с.hidden=!с.querySelector('article.task:not([hidden])');
    cnt.textContent=видно+' из '+карточки.length;
    пусто.hidden=видно>0;
  }
  for(const [ид,ключ] of [['pr','pr'],['st','st']]){
    document.getElementById(ид).addEventListener('click',e=>{
      const c=e.target.closest('.chip'); if(!c) return;
      const v=c.dataset.v, было=выбор[ключ].has(v);
      было?выбор[ключ].delete(v):выбор[ключ].add(v);
      c.setAttribute('aria-pressed',String(!было)); рисуй();
    });
  }
  q.addEventListener('input',рисуй);
  blk.addEventListener('change',рисуй);
  рисуй();
})();
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

    блок_сводки, пр = сводка(задачи)
    ВЫХОД.write_text(
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>Трекер AOCG AI Офис</title>"
        f"{ШРИФТЫ}<style>{СТИЛЬ}</style></head><body>"
        "<header><h1>Трекер AOCG AI Офис</h1>"
        '<p class="sub">Все задачи платформы: что открыто, что закрыто и чем это '
        "подтверждено. Строка раскрывается — внутри разбор находки, замеры "
        "и оговорки.</p>"
        '<div class="src"><span>источник: docs/TASKS.md</span>'
        f"<span>задач {len(задачи)} · собрано командой make tracker</span>"
        "<span>представление, а не копия — в git не хранится</span></div>"
        + блок_сводки
        + "</header>"
        + блок_порядка(порядок_выполнения(текст))
        + панель(разделы, пр, len(задачи))
        + в_html(разделы)
        + f"<script>{СКРИПТ}</script></body></html>",
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
