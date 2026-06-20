"""Профиль организации (задача #1, v1).

2 эндпоинта:
  GET   /api/organizations/me  — профиль СВОЕЙ орг (любой член орг)
  PATCH /api/organizations/me  — правка name/inn (только admin)

Org-scope by design: org_id берётся из get_current_user (токен), НЕ из пути,
поэтому cross-org доступ (IDOR) невозможен — чужой id передать некуда.
Тип организации (person|company) в v1 — только чтение. SQL параметризован.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator

from aocg_security.validators import validate_inn
from app.auth import get_current_user
from app.database import get_pool
from app.routers.auth import _require_admin

router = APIRouter(prefix="/api/organizations", tags=["organizations"])


class OrgUpdateIn(BaseModel):
    # extra="forbid": попытка передать read-only поле (type, owner_id, …) → 422,
    # а не молчаливое игнорирование.
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    inn: Optional[str] = None

    @field_validator("inn")
    @classmethod
    def _check_inn(cls, v):
        # ИНН: 10/12 цифр + контрольная сумма. None = не меняем → пропускаем.
        return validate_inn(v) if v is not None else v


@router.get("/me")
async def get_my_org(user: dict = Depends(get_current_user)):
    """Профиль организации текущего пользователя (type — read-only в v1)."""
    p = await get_pool()
    row = await p.fetchrow(
        "SELECT id, name, inn, type, owner_id, created_at "
        "FROM organizations WHERE id=$1",
        user["org_id"],
    )
    if not row:
        raise HTTPException(status_code=404, detail="Организация не найдена")
    return dict(row)


@router.patch("/me")
async def update_my_org(body: OrgUpdateIn, user: dict = Depends(get_current_user)):
    """Правка названия / ИНН своей орг. Только администратор. Тип не меняется.

    None в поле = не трогать (COALESCE сохраняет текущее значение)."""
    _require_admin(user)
    if body.name is not None and not body.name.strip():
        raise HTTPException(
            status_code=400, detail="Название организации не может быть пустым"
        )
    p = await get_pool()
    row = await p.fetchrow(
        "UPDATE organizations SET name=COALESCE($1,name), inn=COALESCE($2,inn) "
        "WHERE id=$3 "
        "RETURNING id, name, inn, type, owner_id, created_at",
        body.name.strip() if body.name is not None else None,
        body.inn.strip() if body.inn is not None else None,
        user["org_id"],
    )
    if not row:
        raise HTTPException(status_code=404, detail="Организация не найдена")
    return dict(row)
