"""Отправка писем через Yandex Cloud Postbox по SMTP.

ПОЧЕМУ POSTBOX, А НЕ RESEND (S-74, 24.08.2026): Resend — сервис США,
и адреса с именами сотрудников уходили за границу. Postbox держит серверы
в РФ, то есть переход закрывает не только неработающую почту, но и кусок
резидентности данных — ту же задачу, ради которой уезжали с Railway (S-06).

ПОЧЕМУ SMTP, А НЕ AWS-СОВМЕСТИМЫЙ API. У API две поблажки: секрет один
(без производного пароля) и на шаг меньше при настройке. У SMTP одна,
и она перевешивает — ЦЕНА ОТКАТА. SMTP есть у всех поставщиков нашего
короткого списка, и смена поставщика стоит четырёх переменных окружения,
не трогая ни строки кода. SES-совместимый API есть ровно у двоих —
Amazon и Postbox, — и уход к любому российскому означал бы переписать
этот модуль целиком. Поставщика мы за неделю меняли дважды, а
доставляемость Postbox ещё не измерена: сегодня он кандидат, а не решение.

ВСЕ ПАРАМЕТРЫ — ТОЛЬКО ИЗ ОКРУЖЕНИЯ, ни одного значения в коде.
Почта включена, лишь когда заданы ВСЕ обязательные переменные:
частичная настройка — это выключено, а не «почти работает».

⚠️ POSTBOX_SMTP_PASSWORD — НЕ секретный ключ сервисного аккаунта. Пароль
для SMTP получается отдельным генератором Яндекса из этого секрета.
Подставить сам секрет — получить отказ авторизации, на вид неотличимый
от «неверный ключ».
"""

import os
import smtplib
from email.message import EmailMessage

SMTP_HOST = "POSTBOX_SMTP_HOST"
SMTP_PORT = "POSTBOX_SMTP_PORT"
SMTP_USER = "POSTBOX_SMTP_USER"
SMTP_PASSWORD = "POSTBOX_SMTP_PASSWORD"
SMTP_FROM = "POSTBOX_FROM"

# Порт в обязательные НЕ входит: у него есть рабочее умолчание,
# и его отсутствие не делает настройку неполной.
ОБЯЗАТЕЛЬНЫЕ = (SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM)

ПОРТ_ПО_УМОЛЧАНИЮ = 587  # STARTTLS; 465 — SMTPS, если задан явно
ТАЙМАУТ = 20


def _незаданные() -> list:
    return [имя for имя in ОБЯЗАТЕЛЬНЫЕ if not os.getenv(имя)]


def email_enabled() -> bool:
    """Почта включена, только если заданы ВСЕ обязательные переменные."""
    return not _незаданные()


def _send(to_email: str, subject: str, html: str) -> bool:
    """Отправляет письмо. НИКОГДА не бросает исключение наружу.

    Возвращает False и печатает причину — на это опирается регистрация:
    при выключенной почте она подтверждает аккаунт сама (auto_verify),
    и падение здесь уронило бы её целиком.
    """
    отсутствуют = _незаданные()
    if отсутствуют:
        print(f"[EMAIL:disabled] не заданы: {', '.join(отсутствуют)}", flush=True)
        return False

    письмо = EmailMessage()
    письмо["From"] = os.getenv(SMTP_FROM)
    письмо["To"] = to_email
    письмо["Subject"] = subject
    письмо.set_content("Письмо в формате HTML. Откройте его в почтовом клиенте.")
    письмо.add_alternative(html, subtype="html")

    try:
        порт = int(os.getenv(SMTP_PORT) or ПОРТ_ПО_УМОЛЧАНИЮ)
    except ValueError:
        print(f"[EMAIL] {SMTP_PORT} не число — беру {ПОРТ_ПО_УМОЛЧАНИЮ}", flush=True)
        порт = ПОРТ_ПО_УМОЛЧАНИЮ

    try:
        хост = os.getenv(SMTP_HOST)
        if порт == 465:
            сервер = smtplib.SMTP_SSL(хост, порт, timeout=ТАЙМАУТ)
        else:
            сервер = smtplib.SMTP(хост, порт, timeout=ТАЙМАУТ)
            сервер.starttls()
        with сервер:
            сервер.login(os.getenv(SMTP_USER), os.getenv(SMTP_PASSWORD))
            сервер.send_message(письмо)
        return True
    except Exception as e:  # noqa: BLE001 — письмо не должно ломать запрос
        print(f"[EMAIL] send failed: {type(e).__name__}: {e}", flush=True)
        return False


def send_verification_email(to_email: str, verify_url: str, часов: int = 72) -> bool:
    # ⚠️ ССЫЛКУ В ЖУРНАЛ НЕ ПИШЕМ (S-73). Переход по ней возвращает access
    # и refresh токены — то есть строка в журнале была бы готовым входом
    # в чужой аккаунт. Сегодня она не печаталась лишь потому, что при
    # выключенной почте сюда не доходило управление; с Postbox дошло бы.
    html = f"""<div style="font-family:Arial,sans-serif;color:#111318">
      <h2 style="color:#A4161A">Подтвердите email</h2>
      <p>Чтобы активировать аккаунт в AOCG AI Офис, нажмите кнопку.
         Ссылка действует <b>{часов} часа</b> и сработает один раз:</p>
      <p><a href="{verify_url}" style="background:#A4161A;color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;display:inline-block">Подтвердить email</a></p>
      <p style="color:#636B7D;font-size:13px">Или откройте ссылку: {verify_url}</p>
    </div>"""
    return _send(to_email, "Подтвердите email — AOCG AI Офис", html)


def send_password_reset_email(to_email: str, reset_url: str) -> bool:
    """Письмо со ссылкой на смену пароля (S-56).

    ⚠️ СРОК ЖИЗНИ НАЗВАН В САМОМ ПИСЬМЕ, И ЭТО НЕ УКРАШЕНИЕ: человек, увидев
    ссылку через два часа, должен понимать, почему она не работает, — иначе
    он решит, что сломан сервис, и запросит ещё пять писем.

    ⚠️ И ПРЕДУПРЕЖДЕНИЕ ТОМУ, КТО ПИСЬМА НЕ ЗАПРАШИВАЛ: чужой запрос сам
    по себе ничего не ломает, но человек должен знать, что кто-то вводил
    его адрес.
    """
    html = f"""<div style="font-family:Arial,sans-serif;color:#111318">
      <h2 style="color:#A4161A">Смена пароля</h2>
      <p>Кто-то запросил смену пароля для этого адреса в AOCG AI Офис.
         Ссылка действует <b>60 минут</b> и сработает один раз:</p>
      <p><a href="{reset_url}">{reset_url}</a></p>
      <p style="color:#5A6472;font-size:13px">Если вы этого не запрашивали —
         просто не открывайте ссылку. Пароль останется прежним.</p>
    </div>"""
    return _send(to_email, "Смена пароля — AOCG AI Офис", html)


def send_report_status_email(
    to_email: str, отчёт: str, статус: str, причина: str = "", ссылка: str = ""
) -> bool:
    """Письмо о смене статуса отчёта — второй канал того же события (T159).

    ⚠️ ПРИЧИНА ОТКАЗА В ПИСЬМЕ ОБЯЗАТЕЛЬНА — требование владельца 04.09.2026:
    «без причины человек всё равно идёт выяснять, и уведомление не экономит
    ему ничего». Письмо без причины хуже отсутствия письма: оно тревожит
    и не отвечает.

    ⚠️ ОТКЛОНЁН — ГЛАВНОЕ ИЗ ТРЁХ ПИСЕМ, и текст это отражает: одобрение
    приятно, но отказ означает, что человеку НЕ ВЕРНУЛИ ДЕНЬГИ, и до сих пор
    он узнавал об этом, только зайдя в приложение по своей воле.
    """
    отклонён = статус == "Отклонён"
    заголовок = "Отчёт отклонён" if отклонён else "Отчёт одобрен"
    цвет = "#B45309" if отклонён else "#15803D"
    объяснение = (
        f'<p style="background:#FEF3C7;padding:12px 14px;border-radius:8px">'
        f"<b>Причина:</b> {причина}</p>"
        if отклонён
        else "<p>Расходы приняты к возмещению.</p>"
    )
    что_делать = (
        "<p>Исправьте отчёт и отправьте снова — он останется на месте, "
        "заново собирать чеки не нужно.</p>"
        if отклонён
        else ""
    )
    кнопка = (
        f'<p><a href="{ссылка}" style="background:#A4161A;color:#fff;padding:12px 22px;'
        f'border-radius:8px;text-decoration:none;display:inline-block">Открыть отчёт</a></p>'
        if ссылка
        else ""
    )
    html = f"""<div style="font-family:Arial,sans-serif;color:#111318">
      <h2 style="color:{цвет}">{заголовок}</h2>
      <p>Отчёт <b>{отчёт}</b> — {статус.lower()}.</p>
      {объяснение}
      {что_делать}
      {кнопка}
    </div>"""
    return _send(to_email, f"{заголовок}: {отчёт} — AOCG AI Офис", html)


def send_report_submitted_email(
    to_email: str, отчёт: str, автор: str, сумма: str = "", ссылка: str = ""
) -> bool:
    """Управляющему: отчёт пришёл на проверку (T159).

    Без него бухгалтер узнаёт о новом отчёте, только открыв приложение, —
    то есть проверяет вручную каждый день на случай, вдруг что-то пришло.
    """
    строка_суммы = f"<p>Сумма: <b>{сумма}</b></p>" if сумма else ""
    кнопка = (
        f'<p><a href="{ссылка}" style="background:#A4161A;color:#fff;padding:12px 22px;'
        f'border-radius:8px;text-decoration:none;display:inline-block">Посмотреть отчёт</a></p>'
        if ссылка
        else ""
    )
    html = f"""<div style="font-family:Arial,sans-serif;color:#111318">
      <h2 style="color:#A4161A">Отчёт на проверку</h2>
      <p><b>{автор}</b> отправил отчёт <b>{отчёт}</b>.</p>
      {строка_суммы}
      {кнопка}
    </div>"""
    return _send(to_email, f"Отчёт на проверку: {отчёт} — AOCG AI Офис", html)


def send_invite_accepted_email(to_email: str, кто: str, ссылка: str = "") -> bool:
    """Управляющему: приглашённый завёл учётную запись (T159)."""
    кнопка = f'<p><a href="{ссылка}">Список сотрудников</a></p>' if ссылка else ""
    html = f"""<div style="font-family:Arial,sans-serif;color:#111318">
      <h2 style="color:#A4161A">Сотрудник в организации</h2>
      <p><b>{кто}</b> принял приглашение и завёл учётную запись.</p>
      {кнопка}
    </div>"""
    return _send(to_email, "Сотрудник принял приглашение — AOCG AI Офис", html)


def send_fns_data_email(to_email: str, продавец: str, ссылка: str = "") -> bool:
    """Данные по чеку пришли из налоговой (T162).

    ⚠️ ПИСЬМО ДЕРЖИТ ОБЕЩАНИЕ «мы сообщим». Дозапрос идёт при заходе
    человека в приложение, но узнать о результате он должен и НЕ заходя —
    иначе обещание держалось бы на том, что он и так откроет приложение,
    то есть не держалось бы вовсе.
    """
    кнопка = f'<p><a href="{ссылка}">Открыть чек</a></p>' if ссылка else ""
    html = f"""<div style="font-family:Arial,sans-serif;color:#111318">
      <h2 style="color:#15803D">Данные из налоговой получены</h2>
      <p>Чек <b>{продавец}</b> заполнен полностью — проверять руками не нужно.</p>
      {кнопка}
    </div>"""
    return _send(to_email, f"Чек дополнен данными ФНС: {продавец} — AOCG AI Офис", html)


def send_invite_notification(
    to_email: str, invite_url: str, org_name: str, role: str
) -> bool:
    html = f"""<div style="font-family:Arial,sans-serif;color:#111318">
      <h2 style="color:#A4161A">Приглашение в «{org_name}»</h2>
      <p>Вас пригласили в AOCG AI Офис (роль: {role}). Перейдите по ссылке, чтобы присоединиться:</p>
      <p><a href="{invite_url}">{invite_url}</a></p>
    </div>"""
    return _send(to_email, f"Приглашение в «{org_name}» — AOCG AI Офис", html)
