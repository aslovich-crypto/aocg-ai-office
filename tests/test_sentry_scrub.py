"""Тесты before_send для Sentry (app.monitoring).

Матрица заводится с нуля: до 19.08.2026 `_sentry_scrub` не был покрыт
тестами вовсе, хотя это последняя преграда между ПД и сторонним сервисом.

⚠️ ГЛАВНОЕ, ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, — НЕ «УТЕКЛО ЛИ», А «ДОШЛО ЛИ СОБЫТИЕ».
`_sentry_scrub` обёрнут в try/except, и except возвращает None — событие
отбрасывается целиком. Значит ошибка в фильтрации не пропустит ПД, а сделает
нас СЛЕПЫМИ молча. Поэтому в каждом тесте первым делом утверждается, что
событие вернулось, и только потом — что из него вырезано.
"""

import app.monitoring as m
from app.monitoring import _sentry_scrub


def _событие(данные):
    return {"request": {"data": данные, "url": "https://api.aocgai.ru/x"}}


# --- ТЕСТ 1: обычный словарь -------------------------------------------------


def test_1_обычный_словарь_событие_дошло_и_пд_вырезаны():
    e = _sentry_scrub(
        _событие(
            {
                "first_name": "Алексей",
                "last_name": "Шукалович",
                "text": "сводка с именами",
                "role": "admin",
                "ids": [1, 2, 3],
                "password": "длинный-пароль",
            }
        ),
        None,
    )
    assert e is not None, "событие отброшено — мы ослепли, это хуже утечки"
    d = e["request"]["data"]
    assert d["role"] == "admin", "разрешённое перечисление обязано уцелеть"
    assert d["ids"] == [1, 2, 3], "разрешённый список чисел обязан уцелеть"
    assert "Алексей" not in str(d) and "Шукалович" not in str(d)
    assert "сводка" not in str(d)
    assert d["first_name"] == "<вырезано: str, 7>"
    # у секретов форма БЕЗ длины: длина сужает перебор
    assert d["password"] == "<вырезано>"


# --- ТЕСТ 2: AnnotatedValue в headers ---------------------------------------


def test_2_annotated_value_событие_не_отброшено():
    """AnnotatedValue — обёртка библиотеки с метаданными. Она приходит и вместо
    X-Forwarded-For в headers, и вместо самого data, когда Sentry обрезает
    крупное тело. Проверяются ОБА места: в headers (мы их не трогаем) и ВНУТРИ
    data (туда фильтр заходит — там он и может упасть)."""
    from sentry_sdk.utils import AnnotatedValue

    обрезано = AnnotatedValue.removed_because_raw_data()

    e = _sentry_scrub(
        {
            "request": {
                "data": {"role": "admin", "raw_data": обрезано},
                "headers": {"X-Forwarded-For": обрезано},
            }
        },
        None,
    )
    assert e is not None, "событие с AnnotatedValue отброшено — риск сбылся"
    assert e["request"]["data"]["role"] == "admin"

    # data целиком как AnnotatedValue — тоже не должно ронять
    e2 = _sentry_scrub({"request": {"data": обрезано}}, None)
    assert e2 is not None, "событие отброшено, когда data — AnnotatedValue"


# --- ТЕСТ 3: data не словарь -------------------------------------------------


def test_3_data_не_словарь_дошло_И_вырезано():
    """«Дошло» и «вырезано» — РАЗНЫЕ утверждения, проверяются оба.

    До правки 19.08.2026 сырое тело строкой проходило насквозь: разрешение
    выдаётся имени ключа, а у тела на верхнем уровне ключа нет. Это было бы
    дырой шире исходной — ни один ключ такое тело не защищает.
    """
    сырое = "Алексей Шукалович, ИНН 7707083893"
    out = _sentry_scrub(_событие(сырое), None)
    assert out is not None, "событие отброшено"
    assert out["request"]["data"] == "<вырезано: str, 33>"
    assert "Шукалович" not in str(out), "сырое тело прошло насквозь"

    out = _sentry_scrub(_событие(["Алексей", "Шукалович"]), None)
    assert out is not None
    assert "Шукалович" not in str(out), "строка внутри списка прошла насквозь"

    for данные in (None, 42):
        o = _sentry_scrub(_событие(данные), None)
        assert o is not None, f"событие отброшено на data={данные!r}"


# --- ТЕСТ 4: предел глубины --------------------------------------------------


def test_4_предел_глубины_метка_а_не_отброс():
    глубоко = ["x"]
    for _ in range(10):
        глубоко = [глубоко]
    out = _sentry_scrub(_событие(глубоко), None)
    assert out is not None, "глубокая структура отбросила событие"
    assert "слишком глубоко" in str(out["request"]["data"])


# --- ТЕСТ 5: fail-closed при поломке фильтра --------------------------------


def test_5_поломка_фильтра_отбрасывает_событие(monkeypatch):
    """Контракт fail-closed: лучше ослепнуть, чем отправить неотскрабленное."""

    def сломано(*a, **kw):
        raise RuntimeError("проба")

    monkeypatch.setattr(m, "_scrub_request_data", сломано)
    assert _sentry_scrub(_событие({"first_name": "Алексей"}), None) is None


# --- ТЕСТ 6: локальные переменные кадров ВЫКЛЮЧЕНЫ ---------------------------


def test_6_переменные_кадров_не_попадают_в_событие(monkeypatch):
    """Проверяется СОБРАННОЕ СОБЫТИЕ, а не факт передачи параметра.

    Параметр может быть переименован или проигнорирован при смене схемы SDK,
    и тогда «мы его передаём» ничего не значит. Поэтому берутся РЕАЛЬНЫЕ
    аргументы нашего init_sentry, с ними поднимается настоящий клиент,
    роняется исключение с маркером в локальной переменной — и проверяется,
    что маркера в событии нет, а ни у одного кадра нет ключа vars.

    Проба на проде 19.08.2026 нашла текст сообщения именно там — шесть раз,
    при чистом request.data (S-66).
    """
    import sentry_sdk

    аргументы = {}

    def перехват(**kw):
        аргументы.update(kw)

    monkeypatch.setenv("SENTRY_DSN", "https://k@o0.ingest.sentry.io/1")
    monkeypatch.setattr(m.sentry_sdk, "init", перехват)
    assert m.init_sentry() is True, "init_sentry не отработал"
    assert "include_local_variables" in аргументы, "параметр вообще не передан"

    # ⚠️ МАРКЕР СОБИРАЕТСЯ ИЗ ЧАСТЕЙ НАМЕРЕННО. Sentry прикладывает к кадру
    # ИСХОДНЫЕ СТРОКИ вокруг места падения (pre_context/context_line/
    # post_context), и записанный литералом маркер попал бы в событие ИЗ
    # ИСХОДНИКА, а не из переменной. Первый прогон теста именно так и упал:
    # vars уже исчезли, а маркер остался — и без этой сборки тест сообщал бы
    # о утечке там, где её нет.
    маркер = "PROBE" + "-7Q4X-" + "MARKER"

    def уронить():
        секрет = маркер  # noqa: F841 — нужен именно как локальная переменная
        raise RuntimeError("проба")

    # ⚠️ СНЯТЬ ПАТЧ ДО БОЕВОГО ЗАПУСКА. m.sentry_sdk — ТОТ ЖЕ объект модуля,
    # что и sentry_sdk здесь: не сняв патч, мы позвали бы собственный перехват
    # вместо настоящего init, событие не собралось бы, и тест «упал бы»
    # по причине, не имеющей отношения к предмету. Поймано первым же прогоном.
    monkeypatch.undo()

    пойманные = []
    боевые = dict(аргументы)
    боевые.update(
        dsn="https://k@o0.ingest.sentry.io/1",
        transport=lambda ev: пойманные.append(ev),
        integrations=[],
        auto_enabling_integrations=False,
        default_integrations=False,
    )
    try:
        sentry_sdk.init(**боевые)
        try:
            уронить()
        except Exception as e:
            sentry_sdk.capture_exception(e)
        sentry_sdk.flush()
    finally:
        sentry_sdk.init(dsn="")  # не оставлять глобальный клиент включённым

    assert пойманные, "событие не собралось — проверять нечего"
    событие = пойманные[0]
    кадры = событие["exception"]["values"][0]["stacktrace"]["frames"]
    assert кадры, "кадров нет — тест выродился"
    с_переменными = [к for к in кадры if "vars" in к]
    assert not с_переменными, f"у {len(с_переменными)} кадров остались vars"
    assert маркер not in str(событие), "маркер утёк через кадр стека"
