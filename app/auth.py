"""Password hashing + JWT tokens + the get_current_user dependency.

Reads config from env (set these on Railway at cutover):
  JWT_SECRET_KEY, JWT_ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_DAYS
JWT_SECRET_KEY is REQUIRED — the app refuses to start without it (no insecure
default), so production can never accidentally sign tokens with a known key.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

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
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != expected_type:
            return None
        sub = payload.get("sub")
        return int(sub) if sub is not None else None
    except (JWTError, ValueError):
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
    user_id = verify_token(token, "access")
    if user_id is None:
        raise cred_exc
    p = await get_pool()
    row = await p.fetchrow(
        "SELECT * FROM users WHERE id=$1 AND is_active=true", user_id
    )
    if not row:
        raise cred_exc
    return dict(row)
