"""Правдоподобность даты чека (строка 24).

СЛУЧАЙ, РАДИ КОТОРОГО ЭТО НАПИСАНО: 12.08.2026 распознавание вернуло
на сегодняшний чек дату 2024-12-26, а на августовский — 2026-01-12
(перепутаны день и месяц). Оба сохранились МОЛЧА.

Проверка НЕ блокирует — решение владельца: чек может быть и правда старым.
Она обязана дать текст, который человек увидит ДО сохранения.
"""

from datetime import date

from app.date_check import MAX_AGE_DAYS, date_warning

СЕГОДНЯ = date(2026, 8, 12)


def test_normal_date_says_nothing():
    assert date_warning(date(2026, 8, 12), СЕГОДНЯ) is None
    assert date_warning(date(2026, 8, 1), СЕГОДНЯ) is None


def test_empty_date_says_nothing():
    # Отсутствие даты ловит обязательность поля, а не эта проверка.
    assert date_warning(None, СЕГОДНЯ) is None


def test_real_case_from_prod_is_caught():
    """Ровно та дата, что молча легла в базу 12.08.2026 (чек id=72)."""
    w = date_warning(date(2024, 12, 26), СЕГОДНЯ)
    assert w is not None
    assert "26.12.2024" in w


def test_future_date_is_caught():
    w = date_warning(date(2026, 12, 31), СЕГОДНЯ)
    assert w is not None and "будущее" in w
    # Дата обязана быть В ТЕКСТЕ и здесь тоже. Проверка добавлена ПОСЛЕ
    # мутации: она ломала дату в сообщении о будущем, а тесты оставались
    # зелёными — про дату спрашивал только тест старой ветки. Один и тот же
    # заявленный признак проверялся в одной ветке из двух.
    assert "31.12.2026" in w


def test_one_day_ahead_is_tolerated_because_of_time_zones():
    """Допуск в сутки — не щедрость, а одиннадцать часовых поясов.

    Сервер живёт по UTC, чек несёт СТЕННОЕ время кассы: чек, пробитый
    ночью во Владивостоке, по серверным меркам «завтрашний». Без допуска
    предупреждали бы о нормальных чеках — то есть учили бы не смотреть
    на предупреждения.
    """
    assert date_warning(date(2026, 8, 13), СЕГОДНЯ) is None
    assert date_warning(date(2026, 8, 14), СЕГОДНЯ) is not None


def test_boundary_of_age_is_exact():
    # Ровно на пороге — молчим; на сутки дальше — предупреждаем.
    from datetime import timedelta

    ровно_порог = СЕГОДНЯ - timedelta(days=MAX_AGE_DAYS)
    за_порогом = СЕГОДНЯ - timedelta(days=MAX_AGE_DAYS + 1)
    assert date_warning(ровно_порог, СЕГОДНЯ) is None
    assert date_warning(за_порогом, СЕГОДНЯ) is not None


def test_warning_names_the_date_so_it_can_be_checked():
    # Текст без самой даты бесполезен: человек не поймёт, что проверять.
    w = date_warning(date(2020, 1, 5), СЕГОДНЯ)
    assert "05.01.2020" in w
