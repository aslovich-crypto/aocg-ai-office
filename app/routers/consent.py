"""User consent endpoints for 152-ФЗ compliance.

Records the user's affirmative agreement to the privacy policy and personal
data processing terms. Each agreement is an immutable row — re-agreeing
appends a new row rather than updating, so the audit trail is preserved.
The frozen consent_text is stored alongside so an old agreement can be
reproduced verbatim even after the policy is updated.
"""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.database import get_pool

router = APIRouter(prefix="/api/consent", tags=["consent"])

POLICY_VERSION = "1.0"

# Frozen text of the consent at version 1.0. Bump POLICY_VERSION and add a
# new constant when the wording changes — never edit this string in place,
# or historical rows lose their reference text.
CONSENT_TEXT_V1 = """Согласие на обработку персональных данных

Я даю согласие ИП Шукалович Алексей Иванович
(ОГРНИП: 324470400135929, ИНН: 470705591044)
на обработку следующих персональных данных:
ФИО, номер телефона, данные о финансовых операциях —
в целях ведения управленческого учёта в приложении
AOCG AI Офис.

Согласие даётся на срок 5 лет и может быть
отозвано в Настройках приложения.

Версия: 1.0 от 17.05.2026
[PLACEHOLDER — заменить на финальный текст юриста]"""


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


@router.post("/")
async def record_consent(req: ConsentRequest):
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
async def get_latest_consent(user_id: str):
    """Most recent consent for the user, or `null` if none recorded."""
    p = await get_pool()
    row = await p.fetchrow(
        """SELECT id, consent_at, policy_version FROM user_consents
           WHERE user_id=$1 ORDER BY consent_at DESC LIMIT 1""",
        user_id,
    )
    return _serialize(row) if row else None
