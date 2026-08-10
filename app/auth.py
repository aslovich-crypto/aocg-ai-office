"""Password hashing + JWT tokens + the get_current_user dependency.

Reads config from env (set these on Railway at cutover):
  JWT_SECRET_KEY, JWT_ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_DAYS
JWT_SECRET_KEY is REQUIRED — the app refuses to start without it (no insecure
default), so production can never accidentally sign tokens with a known key.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.database import get_pool

# Fail-fast: НЕ подписываем токены публично известным дефолтом. Если ключ не
# задан (Railway variables / локальный .env) — отказываемся стартовать, а не
# молча падаем на угадываемый секрет (иначе любой смог бы подделать JWT).
JWT_SECRET = os.getenv("JWT_SECRET_KEY")
if not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET_KEY не задан — отказ запуска во избежание подписи токенов "
        "небезопасным дефолтом. Задайте переменную окружения JWT_SECRET_KEY."
    )
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

# Роли (строки в users.role) и предикаты доступа к чекам (A-ACL).
# Семантика VIEW/PATCH и DELETE расходится — два разных предиката.
ROLE_ADMIN = "admin"
ROLE_ACCOUNTANT = "accountant"
ROLE_EMPLOYEE = "employee"

# S-24: ЕДИНСТВЕННЫЙ белый список ролей Примы. Всё, что заводит человека
# (приглашение, создание пользователя), обязано валидироваться по нему,
# иначе в users.role приезжает произвольная строка: гейты сравнивают роль
# по равенству, поэтому «Admin» с большой буквы или «суперадмин» тихо
# получают права РЯДОВОГО сотрудника — то есть роль есть, а прав нет,
# и разбираться с этим будут на живом человеке.
# Роль `manager` (РП) из приложения «Финансы» сюда НЕ входит намеренно:
# в Приме её никто не понимает. Добавлять — вместе с FIN-01.
ROLES = (ROLE_ADMIN, ROLE_ACCOUNTANT, ROLE_EMPLOYEE)
Role = Literal["admin", "accountant", "employee"]


def can_see_all(role: str) -> bool:
    """Видит и правит ВСЕ чеки своей орг (admin, accountant). Иначе — только свои."""
    return role in (ROLE_ADMIN, ROLE_ACCOUNTANT)


def can_delete_any(role: str) -> bool:
    """Удаляет ЛЮБЫЕ чеки орг (только admin). accountant удаляет только свои."""
    return role == ROLE_ADMIN


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: Optional[str]) -> bool:
    if not hashed:
        return False
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:  # noqa: BLE001 — malformed hash → treat as mismatch
        return False


def _create_token(user_id: int, kind: str, expires: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "type": kind, "iat": now, "exp": now + expires}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_access_token(user_id: int) -> str:
    return _create_token(
        user_id, "access", timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )


def create_refresh_token(user_id: int) -> str:
    return _create_token(user_id, "refresh", timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))


def verify_token(token: str, expected_type: str = "access") -> Optional[int]:
    """Идентификатор владельца токена или None. Обёртка над разбором ниже —
    оставлена, потому что на неё смотрят вызывающие, которым время выдачи
    не нужно."""
    разобранный = разобрать_токен(token, expected_type)
    return разобранный[0] if разобранный else None


def разобрать_токен(
    token: str, expected_type: str = "access"
) -> Optional[tuple[int, Optional[datetime]]]:
    """(идентификатор, время выдачи) — время нужно для отзыва (S-16).

    Отзыв у нас сделан не чёрным списком, а отметкой `users.tokens_valid_from`:
    токен, выданный ДО отметки, недействителен. Поэтому здесь возвращается
    ещё и `iat`, а не только `sub`.

    `iat` в токенах есть с самого начала (`_create_token`), но у выданных
    ранее он может отсутствовать — в этом случае возвращаем None, и решение
    «что делать со старым токеном» принимает вызывающий, а не разбор.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != expected_type:
            return None
        sub = payload.get("sub")
        if sub is None:
            return None
        iat = payload.get("iat")
        выдан = (
            datetime.fromtimestamp(iat, tz=timezone.utc)
            if isinstance(iat, (int, float))
            else None
        )
        return int(sub), выдан
    except (JWTError, ValueError, OSError, OverflowError):
        return None


async def get_current_user(token: Optional[str] = Depends(oauth2_scheme)) -> dict:
    """Resolve the bearer access token to an active user row, or 401."""
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не авторизован",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise cred_exc
    разобранный = разобрать_токен(token, "access")
    if разобранный is None:
        raise cred_exc
    user_id, выдан = разобранный
    p = await get_pool()
    row = await p.fetchrow(
        "SELECT * FROM users WHERE id=$1 AND is_active=true", user_id
    )
    if not row:
        raise cred_exc

    # ОТЗЫВ ТОКЕНОВ БЕЗ ЧЁРНОГО СПИСКА (S-16). Отметка `tokens_valid_from`
    # приезжает в ТОЙ ЖЕ строке, которую мы и так читаем на каждом запросе,
    # поэтому проверка не стоит ни одного дополнительного обращения к базе.
    # Чёрный список стоил бы второго запроса на КАЖДЫЙ вызов API плюс роста
    # таблицы и чистки — при том, что гасить по одному токену нам нужно
    # только refresh (это уже умеет `revoked_tokens`).
    #
    # Что гасится отметкой: все токены пользователя разом — при смене пароля
    # и по «выйти на всех устройствах».
    #
    # NULL = «никого не выгоняли», и это состояние ПО УМОЛЧАНИЮ после
    # выкатки: ставить отметку всем сразу значило бы разлогинить всех
    # без нужды. Защита начинает действовать с первого отзыва.
    отметка = row["tokens_valid_from"] if "tokens_valid_from" in row else None
    if отметка is not None:
        # СЕКУНДЫ, А НЕ МИКРОСЕКУНДЫ. `iat` в JWT — целое число секунд
        # (так устроен стандарт), а отметка приходит из базы с микросекундами.
        # Без округления токен, выданный В ТУ ЖЕ СЕКУНДУ, что и отзыв,
        # оказывался бы «старше» отметки и умирал сразу после выдачи —
        # поймано тестом «токен, выданный после отзыва, работает», который
        # падал на свежей паре из смены пароля.
        # Цена округления: токен, выданный в ту же секунду ДО отзыва, ещё
        # секунду проживёт. Для «выйти везде» это ничто, а альтернатива —
        # мёртвые токены сразу после выдачи.
        отметка = отметка.replace(microsecond=0)
        # Токен без `iat` (выдан до появления поля) при действующей отметке
        # доверия не заслуживает: доказать, что он новее отзыва, нечем.
        if выдан is None or выдан < отметка:
            raise cred_exc

    return dict(row)
