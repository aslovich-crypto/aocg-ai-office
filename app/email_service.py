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


def send_invite_notification(
    to_email: str, invite_url: str, org_name: str, role: str
) -> bool:
    html = f"""<div style="font-family:Arial,sans-serif;color:#111318">
      <h2 style="color:#A4161A">Приглашение в «{org_name}»</h2>
      <p>Вас пригласили в AOCG AI Офис (роль: {role}). Перейдите по ссылке, чтобы присоединиться:</p>
      <p><a href="{invite_url}">{invite_url}</a></p>
    </div>"""
    return _send(to_email, f"Приглашение в «{org_name}» — AOCG AI Офис", html)
