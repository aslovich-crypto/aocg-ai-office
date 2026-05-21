from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user, hash_password, verify_password
from app.database import get_pool

router = APIRouter(prefix="/api/users", tags=["users"])

# Columns the admin PATCH /{id} (employee management) may touch.
UPDATABLE = ("first_name", "last_name", "patronymic", "email", "inn", "region", "employee_id")

# Never expose secrets in user payloads.
_HIDDEN = ("password_hash", "email_verify_token")


def _safe(row) -> dict:
    return {k: v for k, v in dict(row).items() if k not in _HIDDEN}


async def _me_payload(p, u: dict) -> dict:
    """Profile shape for GET/PATCH /me, including latest consent."""
    # TODO(auth-migration): user_consents с user_id='local_user' — legacy
    # из периода до авторизации. После полного перевода всех users
    # на JWT выполнить миграцию:
    #   SELECT consent → match by email → UPDATE user_id → DELETE legacy.
    # 152-ФЗ требует чёткой привязки согласия к user_id.
    consent_row = await p.fetchrow(
        """SELECT consent_at, policy_version FROM user_consents
           WHERE user_id IN ($1, $2, 'local_user')
           ORDER BY consent_at DESC LIMIT 1""",
        str(u["id"]), (u.get("email") or ""),
    )
    consent = None
    if consent_row:
        consent = {
            "given_at": consent_row["consent_at"].isoformat() if consent_row["consent_at"] else None,
            "policy_version": consent_row["policy_version"],
        }
    return {
        "id": u["id"],
        "first_name": u.get("first_name"),
        "last_name": u.get("last_name"),
        "email": u.get("email"),
        "phone": u.get("phone"),
        "employee_number": u.get("employee_number"),
        "role": u.get("role"),
        "is_email_verified": u.get("is_email_verified"),
        "linked_providers": [],  # OAuth not wired yet
        "consent": consent,
    }


class UserUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    patronymic: Optional[str] = None
    email: Optional[str] = None
    inn: Optional[str] = None
    region: Optional[str] = None
    employee_id: Optional[str] = None


class MeUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    employee_number: Optional[str] = None


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


class UserCreate(BaseModel):
    first_name: str
    last_name: str = ""
    patronymic: str = ""
    email: str = ""
    role: str = "employee"


@router.get("/")
async def get_users(user: dict = Depends(get_current_user)):
    """Active users of the caller's organization, oldest first."""
    p = await get_pool()
    rows = await p.fetch(
        "SELECT * FROM users WHERE is_active = true AND org_id=$1 ORDER BY id", user["org_id"])
    return [_safe(r) for r in rows]


# ─── /me (must be declared before /{id}) ───
@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    """Current user's profile."""
    p = await get_pool()
    return await _me_payload(p, user)


@router.patch("/me")
async def update_me(u: MeUpdate, user: dict = Depends(get_current_user)):
    """Self-service profile edit. Only first_name/last_name/phone/employee_number."""
    fields = {k: v for k, v in u.model_dump(exclude_unset=True).items()
              if k in ("first_name", "last_name", "phone", "employee_number")}
    p = await get_pool()
    if fields:
        cols = list(fields.keys())
        sets = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(cols))
        await p.execute(
            f"UPDATE users SET {sets} WHERE id = ${len(cols) + 1}",
            *[fields[c] for c in cols], user["id"])
    fresh = await p.fetchrow("SELECT * FROM users WHERE id=$1", user["id"])
    return await _me_payload(p, dict(fresh))


@router.post("/me/change-password")
async def change_password(body: PasswordChange, user: dict = Depends(get_current_user)):
    if not verify_password(body.old_password, user.get("password_hash")):
        raise HTTPException(status_code=400, detail="Текущий пароль неверный")
    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Новый пароль должен быть не менее 8 символов")
    p = await get_pool()
    await p.execute("UPDATE users SET password_hash=$1 WHERE id=$2",
                    hash_password(body.new_password), user["id"])
    return {"ok": True}


@router.post("/")
async def create_user(u: UserCreate, user: dict = Depends(get_current_user)):
    """Add an employee directly into the caller's organization."""
    p = await get_pool()
    row = await p.fetchrow(
        """INSERT INTO users (first_name, last_name, patronymic, email, role, org_id)
           VALUES ($1, $2, $3, $4, $5, $6) RETURNING *""",
        u.first_name, u.last_name, u.patronymic, u.email, u.role, user["org_id"],
    )
    return _safe(row)


@router.patch("/{id}")
async def update_user(id: int, u: UserUpdate, user: dict = Depends(get_current_user)):
    fields = {k: v for k, v in u.model_dump(exclude_unset=True).items() if k in UPDATABLE}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    cols = list(fields.keys())
    sets = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(cols))
    p = await get_pool()
    row = await p.fetchrow(
        f"UPDATE users SET {sets} WHERE id = ${len(cols) + 1} AND org_id = ${len(cols) + 2} RETURNING *",
        *[fields[c] for c in cols], id, user["org_id"],
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return _safe(row)


@router.delete("/{id}")
async def deactivate_user(id: int, user: dict = Depends(get_current_user)):
    """Soft-delete within the caller's org: keep the row, flip is_active off."""
    p = await get_pool()
    await p.execute("UPDATE users SET is_active = false WHERE id = $1 AND org_id=$2", id, user["org_id"])
    return {"ok": True}
