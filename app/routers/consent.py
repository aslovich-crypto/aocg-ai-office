"""User consent endpoints for 152-ФЗ compliance.

Records the user's affirmative agreement to the privacy policy and personal
data processing terms. Each agreement is an immutable row — re-agreeing
appends a new row rather than updating, so the audit trail is preserved.
The frozen consent_text is stored alongside so an old agreement can be
reproduced verbatim even after the policy is updated.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import get_current_user
from app.database import get_pool

router = APIRouter(prefix="/api/consent", tags=["consent"])

# ─── ЕДИНСТВЕННЫЙ ИСТОЧНИК ТЕКСТА СОГЛАСИЯ (S-34) ───────────────────────────
# До 07.08.2026 источников было ДВА: эта константа и своя копия во фронте.
# Они разошлись молча — фронт показывал 548 символов с оговоркой о передаче
# фото в Anthropic (США), сюда сохранялось 450 БЕЗ неё, и обе редакции носили
# версию «1.0». Запись фиксировала не то, что человек прочитал, и не
# подтверждала согласие на трансграничную передачу, которую мы выполняем
# при каждом распознавании чека.
#
# Теперь текст живёт ЗДЕСЬ, а фронт получает его ручкой GET /api/consent/policy
# и показывает ровно то, что получил. Копию во фронте не заводить: разойдётся
# снова, и снова молча.
#
# ПРАВИЛО ВЕРСИИ (за ним следит tests/test_consent_policy.py):
#   • изменили текст — ОБЯЗАНЫ поднять версию и вписать новую пару в ЭТАЛОН;
#   • подняли версию — текст ОБЯЗАН отличаться, иначе в журнале появится
#     запись о согласии на редакцию, которой не было.
POLICY_VERSION = "1.1"

# Версия 1.1 = редакция, которую фронт показывал людям с 20.05.2026 (в ней
# есть оговорка о трансграничной передаче), плюс приведённая в соответствие
# строка версии. Записи версии 1.0 остаются в журнале как есть: текст в них
# заморожен намеренно, переписывать историю нельзя.
CONSENT_TEXT = """Согласие на обработку персональных данных

Я даю согласие ИП Шукалович Алексей Иванович
(ОГРНИП: 324470400135929, ИНН: 470705591044)
на обработку следующих персональных данных:
ФИО, номер телефона, данные о финансовых операциях —
в целях ведения управленческого учёта.

Я уведомлён, что для распознавания фото чеков
изображения передаются сервису Anthropic PBC (США)
и не сохраняются третьими лицами.

Согласие даётся на срок 5 лет и может быть
отозвано в Настройках приложения.

Версия: 1.1 от 07.08.2026

[PLACEHOLDER — финальная редакция юриста]"""

# Старое имя оставлено ссылкой: на него смотрели тесты и, возможно, скрипты.
CONSENT_TEXT_V1 = CONSENT_TEXT


class ConsentRequest(BaseModel):
    user_id: str
    ip_address: Optional[str] = None


def _serialize(row) -> dict:
    """Trim a user_consents row to the public response shape."""
    return {
        "id": row["id"],
        "consent_at": row["consent_at"].isoformat() if row["consent_at"] else None,
        "policy_version": row["policy_version"],
    }


# ВАЖЕН ПОРЯДОК: «/policy» объявлен ДО «/{user_id}», иначе путь совпадёт
# с параметром и ручка политики станет недостижимой (тот же случай, что
# «/me» перед «/{id}» в users.py).
@router.get("/policy")
async def get_policy(user: dict = Depends(get_current_user)):
    """Текст согласия и его версия — то, что фронт обязан показать.

    Закрыта авторизацией намеренно: экран согласия показывается ПОСЛЕ входа,
    поэтому публичной ручке здесь взяться неоткуда, а лишняя открытая ручка
    это лишняя поверхность (S-33).
    """
    return {"version": POLICY_VERSION, "text": CONSENT_TEXT}


@router.post("/")
async def record_consent(req: ConsentRequest, user: dict = Depends(get_current_user)):
    """Append a new consent row for `user_id`. Always inserts — re-agreement is intentional."""
    p = await get_pool()
    row = await p.fetchrow(
        """INSERT INTO user_consents (user_id, ip_address, policy_version, consent_text)
           VALUES ($1, $2, $3, $4)
           RETURNING id, consent_at, policy_version""",
        req.user_id,
        req.ip_address,
        POLICY_VERSION,
        CONSENT_TEXT_V1,
    )
    return _serialize(row)


@router.get("/{user_id}")
async def get_latest_consent(user_id: str, user: dict = Depends(get_current_user)):
    """Most recent consent for the user, or `null` if none recorded."""
    p = await get_pool()
    row = await p.fetchrow(
        """SELECT id, consent_at, policy_version FROM user_consents
           WHERE user_id=$1 ORDER BY consent_at DESC LIMIT 1""",
        user_id,
    )
    return _serialize(row) if row else None
