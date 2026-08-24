"""Тесты отправителя писем (app.email_service, Yandex Cloud Postbox по SMTP).

⚠️ ГЛАВНОЕ, ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ, — НЕ «УХОДИТ ЛИ ПИСЬМО», А «НЕ ЛОМАЕТ ЛИ
ОТСУТСТВИЕ ПОЧТЫ РЕГИСТРАЦИЮ». На выключенной почте держится auto_verify:
при пустых переменных регистрация обязана пройти и подтвердить аккаунт сама.
Если _send начнёт бросать исключение, регистрация упадёт 500 — и это будет
отказ продукта из-за ненастроенной второстепенной службы.

Настоящую отправку тесты не делают: сети в прогоне нет и быть не должно.
"""

import app.email_service as es

ПЕРЕМЕННЫЕ = (
    "POSTBOX_SMTP_HOST",
    "POSTBOX_SMTP_USER",
    "POSTBOX_SMTP_PASSWORD",
    "POSTBOX_FROM",
)


def _очистить(monkeypatch):
    for имя in ПЕРЕМЕННЫЕ + ("POSTBOX_SMTP_PORT",):
        monkeypatch.delenv(имя, raising=False)


def _заполнить(monkeypatch):
    monkeypatch.setenv("POSTBOX_SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("POSTBOX_SMTP_USER", "проба-логин")
    monkeypatch.setenv("POSTBOX_SMTP_PASSWORD", "проба-пароль")
    monkeypatch.setenv("POSTBOX_FROM", "AOCG <noreply@aocgai.ru>")


# --- выключенное состояние -------------------------------------------------


def test_пустые_переменные_почта_выключена(monkeypatch):
    _очистить(monkeypatch)
    assert es.email_enabled() is False


def test_частичная_настройка_это_выключено_а_не_почти_работает(monkeypatch):
    """Три из четырёх — всё ещё выключено. Иначе получим попытку отправки
    с недостающим параметром и отказ, похожий на поломку сети."""
    _очистить(monkeypatch)
    for имя in ПЕРЕМЕННЫЕ[:3]:
        monkeypatch.setenv(имя, "значение")
    assert es.email_enabled() is False


def test_все_переменные_почта_включена(monkeypatch):
    _заполнить(monkeypatch)
    assert es.email_enabled() is True


# --- ГЛАВНОЕ: отсутствие почты не ломает вызывающего ------------------------


def test_send_возвращает_false_и_называет_каких_переменных_нет(monkeypatch, capfd):
    _очистить(monkeypatch)
    итог = es._send("kto@example.com", "Тема", "<b>тело</b>")
    assert итог is False, "_send обязан вернуть False, а не бросить исключение"
    вывод = capfd.readouterr().out
    assert "[EMAIL:disabled]" in вывод
    for имя in ПЕРЕМЕННЫЕ:
        assert имя in вывод, f"в причине не названа переменная {имя}"


def test_публичные_функции_не_бросают_при_выключенной_почте(monkeypatch):
    """Их зовёт auth.py напрямую. Исключение отсюда = 500 на регистрации."""
    _очистить(monkeypatch)
    assert es.send_verification_email("kto@example.com", "https://app/verify") is False
    assert (
        es.send_invite_notification("kto@example.com", "https://app/j", "Орг", "admin")
        is False
    )


def test_ошибка_отправки_не_бросает_наружу(monkeypatch, capfd):
    """Хост заведомо несуществующий: smtplib бросит, наружу должен выйти False."""
    _заполнить(monkeypatch)
    monkeypatch.setenv("POSTBOX_SMTP_HOST", "nesushchestvuyushchiy.invalid")
    monkeypatch.setenv("POSTBOX_SMTP_PORT", "587")
    assert es._send("kto@example.com", "Тема", "<b>тело</b>") is False
    assert "[EMAIL] send failed:" in capfd.readouterr().out


# --- S-73: ссылка не попадает в журнал -------------------------------------


def test_ссылка_подтверждения_не_печатается_в_журнал(monkeypatch, capfd):
    """S-73: переход по ссылке возвращает токены доступа, поэтому в журнале
    ей не место. До 24.08.2026 она печаталась первой строкой функции."""
    _очистить(monkeypatch)
    ссылка = "https://app.aocgai.ru/verify-email?token=SEKRETNIY-TOKEN-PROBA"
    es.send_verification_email("kto@example.com", ссылка)
    assert "SEKRETNIY-TOKEN-PROBA" not in capfd.readouterr().out


# --- порт ------------------------------------------------------------------


def test_нечисловой_порт_не_роняет_отправку(monkeypatch, capfd):
    _заполнить(monkeypatch)
    monkeypatch.setenv("POSTBOX_SMTP_HOST", "nesushchestvuyushchiy.invalid")
    monkeypatch.setenv("POSTBOX_SMTP_PORT", "не-число")
    assert es._send("kto@example.com", "Тема", "<b>тело</b>") is False
    assert "не число" in capfd.readouterr().out
