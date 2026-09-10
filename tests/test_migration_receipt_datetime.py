# -*- coding: utf-8 -*-
"""DDL времени чека не разойдётся с init_db (строка 20, 11.09.2026).

⚠️ ТОТ ЖЕ ПРИЁМ, ЧТО У `test_migration_people_fields.py` и
`test_migration_invite_columns.py`. Общее правило: строка, прогнанная
на бастионе, обязана лежать в `init_db` ДОСЛОВНО — иначе проверяли бы
одно, а применяли другое.

⚠️ ЧЕМ ЭТА МИГРАЦИЯ ОПАСНЕЕ ПРЕДЫДУЩИХ, И ПОЧЕМУ ТЕСТОВ ЗДЕСЬ ТРИ.
① `ALTER COLUMN ... TYPE` МЕНЯЕТ ДАННЫЕ, а не только схему, и делает это
   МОЛЧА: без `USING ... AT TIME ZONE 'UTC'` результат зависит от пояса
   сессии. Замер 11.09.2026 на живом PostgreSQL: при `TZ=Europe/Moscow`
   голый ALTER сдвинул значения на три часа и отработал без единой ошибки.
   Отсюда тест ②: `USING` обязан быть в обоих местах.
② `ALTER TYPE` не имеет формы `IF NOT EXISTS`, а `init_db` крутится на
   КАЖДОМ старте. Со второго запуска колонка уже без зоны, и то же
   `USING ... AT TIME ZONE 'UTC'` означало бы уже ДРУГОЕ — навесить зону
   обратно. Отсюда тест ③: смена типа обязана стоять под условием.
"""

import importlib.util
import pathlib
import re

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[1]


def _модуль_валидатора():
    спец = importlib.util.spec_from_file_location(
        "валидатор_времени", КОРЕНЬ / "scripts/validate_receipt_datetime.py"
    )
    м = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(м)
    return м


def _схлопнуть(с: str) -> str:
    return re.sub(r"\s+", " ", с).strip()


def _исходник() -> str:
    return (КОРЕНЬ / "app/database.py").read_text(encoding="utf-8")


def test_строка_миграции_есть_в_init_db():
    """⚠️ СРАВНЕНИЕ ПО ВСЕМУ ФАЙЛУ, А НЕ ПОСТРОЧНО.

    У соседних миграций строки короткие и умещаются в одну; здесь DDL
    многострочный, и построчная сверка (как в people_fields) не нашла бы
    его никогда — она была бы ЗЕЛЁНОЙ при отсутствующей миграции.
    """
    м = _модуль_валидатора()
    assert м.МИГРАЦИЯ, "список МИГРАЦИЯ пуст — проверять нечего"
    внутри = _схлопнуть(_исходник())
    пропали = [d for d in м.МИГРАЦИЯ if _схлопнуть(d) not in внутри]
    assert not пропали, (
        "В app/database.py нет дословных строк из "
        "scripts/validate_receipt_datetime.py:\n  " + "\n  ".join(пропали)
    )


def test_using_обязателен_в_обоих_местах():
    """Без USING результат зависит от пояса сессии — и молча.

    Проверяются ОБА места сразу: и рельсы, и `init_db`. Разойдись они —
    валидация на бастионе доказывала бы безопасность одной команды,
    а приложение выполняло бы другую.
    """
    м = _модуль_валидатора()
    for ddl in м.МИГРАЦИЯ:
        if "TYPE TIMESTAMP WITHOUT TIME ZONE" in _схлопнуть(ddl):
            assert "USING" in ddl and "AT TIME ZONE 'UTC'" in ddl, (
                "смена типа без USING ... AT TIME ZONE 'UTC': на кластере "
                "с непустым поясом значения уедут, и ALTER не пожалуется"
            )
    внутри = _схлопнуть(_исходник())
    голый = (
        "ALTER COLUMN datetime TYPE TIMESTAMP WITHOUT TIME ZONE "
        "USING datetime AT TIME ZONE 'UTC'"
    )
    assert голый in внутри, "в init_db смена типа обязана нести USING"


def test_смена_типа_идемпотентна():
    """`ALTER TYPE` не имеет `IF NOT EXISTS` — значит нужно условие.

    ⚠️ ПРОВЕРЯЕТСЯ ФОРМА, А НЕ ПОВЕДЕНИЕ, и границу надо знать: тест читает
    текст DDL, а не гоняет его дважды по базе. Он ловит КЛАСС — смену типа,
    поставленную в `init_db` без защиты от второго прохода. Настоящую
    повторяемость доказывает старт приложения на живой базе.
    """
    внутри = _схлопнуть(_исходник())
    i = внутри.find("ALTER COLUMN datetime TYPE TIMESTAMP WITHOUT TIME ZONE")
    assert i > 0, "смены типа в init_db нет вовсе"
    перед = внутри[max(0, i - 400) : i]
    assert (
        "information_schema.columns" in перед and "timestamp with time zone" in перед
    ), (
        "смена типа стоит без условия по information_schema: на втором "
        "старте она выполнится снова, и USING ... AT TIME ZONE 'UTC' у "
        "колонки БЕЗ зоны навесит зону обратно"
    )
