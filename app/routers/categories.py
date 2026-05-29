"""CRUD справочника категорий расходов (Фикс №1, фаза C).

Справочник per-org (см. categories_seed). 5 эндпоинтов:
  GET    /api/categories                  — группы + статьи орг (вложенно)
  POST   /api/categories                  — создать статью (is_default=FALSE)
  PATCH  /api/categories/{id}             — переименовать / сменить tax_kind (не-системные)
  DELETE /api/categories/{id}             — удалить (не-системные, 409 если есть чеки)
  PATCH  /api/categories/{id}/visibility  — скрыть / показать (любую)

Мутации — только admin/accountant (_require_category_manager). GET — любой член орг.
Всё org-scoped (org_id из get_current_user, НЕ из payload). SQL параметризован.
"""
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user
from app.categories_seed import TAX_KINDS
from app.database import get_pool

router = APIRouter(prefix="/api/categories", tags=["categories"])

# Дефолтный вид расхода при создании статьи без явного tax_kind (Q4).
DEFAULT_TAX_KIND = "Прочие расходы"


def _require_category_manager(user: dict):
    """Мутации справочника — только администратор или бухгалтер (Фикс №1 фаза C, вариант 1).

    Единый гейт для POST/PATCH/DELETE/visibility (образец — _require_admin в auth.py).
    GET остаётся открытым для всех ролей орг."""
    if user.get("role") not in ("admin", "accountant"):
        raise HTTPException(status_code=403, detail="Только для администратора или бухгалтера")


def _validate_tax_kind(tax_kind: str):
    """tax_kind обязан быть одним из 9 (зеркало CHECK-констрейнта) — ловим в коде → 400,
    не доводим до DB-CHECK (500). В сообщении — допустимые значения."""
    if tax_kind not in TAX_KINDS:
        raise HTTPException(
            status_code=400,
            detail="Недопустимый вид расхода. Допустимые: " + ", ".join(TAX_KINDS))


class CategoryIn(BaseModel):
    name: str
    group_id: int
    tax_kind: Optional[str] = None


class CategoryPatch(BaseModel):
    name: Optional[str] = None
    tax_kind: Optional[str] = None


class VisibilityPatch(BaseModel):
    is_visible: bool


@router.get("/")
async def get_categories(visible_only: bool = False, user: dict = Depends(get_current_user)):
    """Группы + вложенные статьи орг. visible_only=true → только видимые (для формы чека)."""
    p = await get_pool()
    groups = await p.fetch(
        "SELECT id, name, position FROM category_groups WHERE org_id=$1 ORDER BY position, id",
        user["org_id"])
    cats = await p.fetch(
        """SELECT id, group_id, name, tax_kind, position, is_default, is_visible
           FROM categories WHERE org_id=$1 ORDER BY position, id""", user["org_id"])
    by_group: dict = {}
    for c in cats:
        if visible_only and not c["is_visible"]:
            continue
        by_group.setdefault(c["group_id"], []).append({
            "id": c["id"], "name": c["name"], "tax_kind": c["tax_kind"],
            "position": c["position"], "is_default": c["is_default"],
            "is_visible": c["is_visible"]})
    return {"groups": [
        {"id": g["id"], "name": g["name"], "position": g["position"],
         "categories": by_group.get(g["id"], [])}
        for g in groups]}


@router.post("/")
async def create_category(c: CategoryIn, user: dict = Depends(get_current_user)):
    """Создать пользовательскую статью (is_default=FALSE) внутри существующей группы орг."""
    _require_category_manager(user)
    tax_kind = c.tax_kind or DEFAULT_TAX_KIND
    _validate_tax_kind(tax_kind)
    p = await get_pool()
    # Группа обязана принадлежать орг вызывающего — иначе чужая/несуществующая → 404.
    grp = await p.fetchrow(
        "SELECT id FROM category_groups WHERE id=$1 AND org_id=$2", c.group_id, user["org_id"])
    if not grp:
        raise HTTPException(status_code=404, detail="Группа не найдена")
    pos = await p.fetchval(
        "SELECT COALESCE(MAX(position),0)+1 FROM categories WHERE group_id=$1 AND org_id=$2",
        c.group_id, user["org_id"])
    try:
        row = await p.fetchrow(
            """INSERT INTO categories
                   (org_id, group_id, name, tax_kind, position, is_default, is_visible)
               VALUES ($1,$2,$3,$4,$5,FALSE,TRUE) RETURNING *""",
            user["org_id"], c.group_id, c.name, tax_kind, pos)
    except asyncpg.exceptions.UniqueViolationError:
        # UNIQUE(org_id,name) — дубль имени отдаём как 409, не 500.
        raise HTTPException(status_code=409, detail="Категория с таким названием уже существует")
    return dict(row)


@router.patch("/{id}")
async def update_category(id: int, c: CategoryPatch, user: dict = Depends(get_current_user)):
    """Переименовать / сменить tax_kind. Только не-системные (is_default=FALSE)."""
    _require_category_manager(user)
    p = await get_pool()
    existing = await p.fetchrow(
        "SELECT id, is_default FROM categories WHERE id=$1 AND org_id=$2", id, user["org_id"])
    if not existing:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    if existing["is_default"]:
        raise HTTPException(status_code=403, detail="Системную категорию нельзя переименовать")
    fields: dict = {}
    if c.name is not None:
        fields["name"] = c.name
    if c.tax_kind is not None:
        _validate_tax_kind(c.tax_kind)
        fields["tax_kind"] = c.tax_kind
    if not fields:
        raise HTTPException(status_code=400, detail="Нет полей для изменения")
    cols = list(fields.keys())
    sets = ", ".join(f"{col}=${i + 1}" for i, col in enumerate(cols))
    try:
        row = await p.fetchrow(
            f"UPDATE categories SET {sets} WHERE id=${len(cols) + 1} AND org_id=${len(cols) + 2} RETURNING *",
            *[fields[col] for col in cols], id, user["org_id"])
    except asyncpg.exceptions.UniqueViolationError:
        raise HTTPException(status_code=409, detail="Категория с таким названием уже существует")
    return dict(row)


@router.delete("/{id}")
async def delete_category(id: int, user: dict = Depends(get_current_user)):
    """Удалить не-системную статью. 403 системная → 409 если к ней привязаны чеки (Q4 порядок)."""
    _require_category_manager(user)
    p = await get_pool()
    existing = await p.fetchrow(
        "SELECT id, is_default FROM categories WHERE id=$1 AND org_id=$2", id, user["org_id"])
    if not existing:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    if existing["is_default"]:
        raise HTTPException(status_code=403, detail="Системную категорию нельзя удалить")
    cnt = await p.fetchval(
        "SELECT COUNT(*) FROM receipts WHERE category_id=$1 AND org_id=$2", id, user["org_id"])
    if cnt > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "category_has_receipts",
                "message": "Нельзя удалить категорию: к ней привязаны чеки",
                "count": cnt,
            })
    await p.execute("DELETE FROM categories WHERE id=$1 AND org_id=$2", id, user["org_id"])
    return {"ok": True}


@router.patch("/{id}/visibility")
async def set_category_visibility(
        id: int, body: VisibilityPatch, user: dict = Depends(get_current_user)):
    """Скрыть/показать ЛЮБУЮ категорию (системную тоже). Скрытие с чеками разрешено (Q6)."""
    _require_category_manager(user)
    p = await get_pool()
    row = await p.fetchrow(
        "UPDATE categories SET is_visible=$1 WHERE id=$2 AND org_id=$3 RETURNING *",
        body.is_visible, id, user["org_id"])
    if not row:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    return dict(row)
