from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user
from app.database import get_pool

router = APIRouter(prefix="/api/users", tags=["users"])

# Whitelist of columns a PATCH may touch — also guards the dynamic UPDATE below
# (keys come from this model, never raw client input).
UPDATABLE = ("first_name", "last_name", "patronymic", "email", "inn", "region", "employee_id")

# Never expose secrets in user payloads.
_HIDDEN = ("password_hash", "email_verify_token")


def _safe(row) -> dict:
    return {k: v for k, v in dict(row).items() if k not in _HIDDEN}


class UserUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    patronymic: Optional[str] = None
    email: Optional[str] = None
    inn: Optional[str] = None
    region: Optional[str] = None
    employee_id: Optional[str] = None


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


@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    """The currently authenticated user."""
    return _safe(user)


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
