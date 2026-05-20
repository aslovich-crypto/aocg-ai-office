from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import get_pool

router = APIRouter(prefix="/api/users", tags=["users"])

# Whitelist of columns a PATCH may touch — also guards the dynamic UPDATE below
# (keys come from this model, never raw client input).
UPDATABLE = ("first_name", "last_name", "patronymic", "email", "inn", "region", "employee_id")


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
async def get_users():
    """All active users/employees, oldest first (admin seeded as id=1)."""
    p = await get_pool()
    rows = await p.fetch("SELECT * FROM users WHERE is_active = true ORDER BY id")
    return [dict(r) for r in rows]


@router.get("/me")
async def get_me():
    """Current user. No auth yet — always the seeded admin (id=1)."""
    p = await get_pool()
    row = await p.fetchrow("SELECT * FROM users WHERE id = 1")
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)


@router.post("/")
async def create_user(u: UserCreate):
    p = await get_pool()
    row = await p.fetchrow(
        """INSERT INTO users (first_name, last_name, patronymic, email, role)
           VALUES ($1, $2, $3, $4, $5) RETURNING *""",
        u.first_name, u.last_name, u.patronymic, u.email, u.role,
    )
    return dict(row)


@router.patch("/{id}")
async def update_user(id: int, u: UserUpdate):
    fields = {k: v for k, v in u.model_dump(exclude_unset=True).items() if k in UPDATABLE}
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    cols = list(fields.keys())
    sets = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(cols))
    p = await get_pool()
    row = await p.fetchrow(
        f"UPDATE users SET {sets} WHERE id = ${len(cols) + 1} RETURNING *",
        *[fields[c] for c in cols], id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)


@router.delete("/{id}")
async def deactivate_user(id: int):
    """Soft-delete: keep the row, just flip is_active off."""
    p = await get_pool()
    await p.execute("UPDATE users SET is_active = false WHERE id = $1", id)
    return {"ok": True}
