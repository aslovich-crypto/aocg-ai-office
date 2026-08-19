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
