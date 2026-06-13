"""Email sending via Resend, with a no-key fallback.

When RESEND_API_KEY is unset (current state — no Resend account yet), nothing is
sent: the verification link is just logged, and callers auto-verify the account
so the flow stays usable. Once RESEND_API_KEY + a verified sending domain are
configured, real emails go out and the normal verify-by-link flow applies.
"""

import os

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = os.getenv("EMAIL_FROM", "AOCG AI Офис <noreply@aocg.ru>")


def email_enabled() -> bool:
    return bool(RESEND_API_KEY)


def _send(to_email: str, subject: str, html: str) -> bool:
    if not RESEND_API_KEY:
        print(f"[EMAIL:disabled] to={to_email} subj={subject!r}", flush=True)
        return False
    try:
        import resend

        resend.api_key = RESEND_API_KEY
        resend.Emails.send(
            {"from": FROM_EMAIL, "to": [to_email], "subject": subject, "html": html}
        )
        return True
    except Exception as e:  # noqa: BLE001 — never let email break the request
        print(f"[EMAIL] send failed: {type(e).__name__}: {e}", flush=True)
        return False


def send_verification_email(to_email: str, verify_url: str) -> bool:
    print(f"[EMAIL] verify link for {to_email}: {verify_url}", flush=True)
    html = f"""<div style="font-family:Arial,sans-serif;color:#111318">
      <h2 style="color:#A4161A">Подтвердите email</h2>
      <p>Чтобы активировать аккаунт в AOCG AI Офис, нажмите кнопку:</p>
      <p><a href="{verify_url}" style="background:#A4161A;color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;display:inline-block">Подтвердить email</a></p>
      <p style="color:#636B7D;font-size:13px">Или откройте ссылку: {verify_url}</p>
    </div>"""
    return _send(to_email, "Подтвердите email — AOCG AI Офис", html)


def send_invite_notification(
    to_email: str, invite_url: str, org_name: str, role: str
) -> bool:
    html = f"""<div style="font-family:Arial,sans-serif;color:#111318">
      <h2 style="color:#A4161A">Приглашение в «{org_name}»</h2>
      <p>Вас пригласили в AOCG AI Офис (роль: {role}). Перейдите по ссылке, чтобы присоединиться:</p>
      <p><a href="{invite_url}">{invite_url}</a></p>
    </div>"""
    return _send(to_email, f"Приглашение в «{org_name}» — AOCG AI Офис", html)
