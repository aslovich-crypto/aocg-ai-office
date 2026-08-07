"""User consent endpoints for 152-ФЗ compliance.

Records the user's affirmative agreement to the privacy policy and personal
data processing terms. Each agreement is an immutable row — re-agreeing
appends a new row rather than updating, so the audit trail is preserved.
The frozen consent_text is stored alongside so an old agreement can be
reproduced verbatim even after the policy is updated.
"""

import pathlib

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from aocg_security.middleware import AOCGSecurityMiddleware
from app.auth import get_current_user
from app.database import get_pool

router = APIRouter(prefix="/api/consent", tags=["consent"])

# ─── ЕДИНСТВЕННЫЙ ИСТОЧНИК ЮРИДИЧЕСКИХ ТЕКСТОВ (S-34, S-36) ─────────────────
# Тексты НЕ дублируются в коде: они ЧИТАЮТСЯ ИЗ ФАЙЛОВ ЮРИСТА в docs/.
# Копия в константе — это ещё одна редакция, которая однажды разойдётся
# с оригиналом; именно так фронт и бэкенд разъехались до 07.08.2026 (обе
# редакции носили номер «1.0», а в журнал сохранялась та, которой человек
# не видел). Файл в docs/ — оригинал, всё остальное только читает.
#
# Отсюда же текст уходит В ЖУРНАЛ при записи согласия и НА ЭКРАН через
# GET /api/consent/policy — одна строка на оба пути, разойтись нечему.
#
# ПРАВИЛО ВЕРСИИ (следит tests/test_consent_policy.py):
#   • изменили текст — обязаны поднять версию и вписать пару в ЭТАЛОН;
#   • подняли версию — текст обязан отличаться, иначе в журнале появится
#     запись о согласии на редакцию, которой не было.
#
# ВЕРСИЯ 2.0 — первая редакция ЮРИСТА (07.08.2026). Нумерация пришла из самих
# документов, а не придумана нами. Записей прежних версий в журнале нет:
# он очищен как тестовый, поэтому «догонять» историю не требуется.
POLICY_VERSION = "2.0"

_ДОКУМЕНТЫ = pathlib.Path(__file__).resolve().parents[2] / "docs"
_ФАЙЛ_СОГЛАСИЯ = _ДОКУМЕНТЫ / "AOCG-LEGAL-002-Soglasie_PD_v2.0.md"
_ФАЙЛ_ПОЛИТИКИ = _ДОКУМЕНТЫ / "AOCG-LEGAL-003-Politika_v2.0.md"


def _прочитать(путь: pathlib.Path) -> str:
    """Прочитать юридический текст или ОТКАЗАТЬСЯ СТАРТОВАТЬ.

    Fail-fast намеренно, как с JWT_SECRET_KEY: приложение без текста согласия
    собирало бы согласия «ни на что» — пустая строка ушла бы и на экран,
    и в журнал. Лучше не подняться, чем принимать юридически пустые записи.
    """
    try:
        текст = путь.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise RuntimeError(
            f"Не найден юридический документ {путь.name}: {e}. "
            "Тексты согласия и политики читаются из docs/ — без них старт "
            "запрещён, иначе согласия собирались бы на пустой текст."
        ) from e
    if len(текст) < 500:
        raise RuntimeError(
            f"{путь.name}: {len(текст)} символов — для юридического документа "
            "подозрительно мало, похоже на обрезанный файл. Старт запрещён."
        )
    return текст


CONSENT_TEXT = _прочитать(_ФАЙЛ_СОГЛАСИЯ)
POLICY_TEXT = _прочитать(_ФАЙЛ_ПОЛИТИКИ)

# Старое имя оставлено ссылкой: на него смотрели тесты и, возможно, скрипты.
CONSENT_TEXT_V1 = CONSENT_TEXT


class ConsentRequest(BaseModel):
    """Тело запроса — ПУСТОЕ по существу (строка 9).

    Раньше здесь были `user_id` и `ip_address`, и оба приходили от клиента.
    Это неверно дважды: субъекта в юридической записи определяет ТОКЕН,
    а адрес — сервер, который видит соединение. Клиент не может быть
    источником доказательства о самом себе.

    Поля намеренно не объявлены: Pydantic отбрасывает лишнее, поэтому
    старый фронт, который ещё шлёт `{"user_id": "local_user"}`, не сломается —
    его данные просто не попадут в запись.
    """


def _адрес(request: Request) -> str:
    """Адрес клиента ОДНИМ способом со всем остальным приложением.

    Намеренно переиспользуется извлекатель ограничителя частоты, а не пишется
    свой: когда S-35 научит брать адрес с правого конца X-Forwarded-For,
    почина должна быть одна на оба места. Вторая копия разошлась бы —
    ровно как разошлись две копии текста согласия (S-34).
    """
    return AOCGSecurityMiddleware._client_ip(request)


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
    return {
        "version": POLICY_VERSION,
        "text": CONSENT_TEXT,
        # Политика — неотъемлемая часть Согласия (п. 8 самого документа),
        # поэтому отдаётся той же ручкой и той же версией: показывать их
        # из разных источников значит снова завести две редакции.
        "policy": POLICY_TEXT,
    }


@router.post("/")
async def record_consent(
    request: Request,
    req: ConsentRequest,
    user: dict = Depends(get_current_user),
):
    """Записать согласие ТЕКУЩЕГО пользователя. Всегда INSERT: повторное
    согласие — новая строка, историю не переписываем.

    СТРОКА 9. Субъект берётся из токена, адрес — из запроса. До 07.08.2026
    оба приходили из тела, и журнал это показал: девятнадцать записей
    с `user_id='local_user'` и пустым адресом, то есть из пяти реквизитов
    152-ФЗ работали два. Те записи удалены как тестовые.
    """
    субъект = str(user["id"])
    if not субъект.isdigit():
        # Страховка, а не рабочая ветка: 'local_user' и любая другая строка
        # вместо идентификатора означают, что субъект пришёл не из токена.
        # Лучше отказать, чем записать в юридический журнал непонятно кого.
        raise HTTPException(status_code=500, detail="Субъект согласия не определён")
    p = await get_pool()
    row = await p.fetchrow(
        """INSERT INTO user_consents (user_id, ip_address, policy_version, consent_text)
           VALUES ($1, $2, $3, $4)
           RETURNING id, consent_at, policy_version""",
        субъект,
        _адрес(request),
        POLICY_VERSION,
        CONSENT_TEXT,
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
