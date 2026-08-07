from fastapi import APIRouter, Depends, HTTPException
from app.auth import get_current_user
from app.database import get_pool
from pydantic import BaseModel

router = APIRouter(prefix="/api/cards", tags=["cards"])


def _require_card_manager(user: dict):
    """Мутации справочника карт — администратор или бухгалтер (S-29).

    Тот же круг, что у справочника категорий (_require_category_manager
    в categories.py), и по той же причине: карты — бухгалтерский справочник
    способов оплаты, его ведёт тот, кто сводит отчёты. Сотруднику карты нужны
    только для выбора при вводе чека, поэтому GET остаётся открытым всем.
    Почему НЕ admin-only, в отличие от users: удаление карты не выдаёт доступ
    и не трогает персональные данные — это правка справочника, откатываемая
    созданием карты заново.
    """
    if user.get("role") not in ("admin", "accountant"):
        raise HTTPException(
            status_code=403, detail="Только для администратора или бухгалтера"
        )


class CardIn(BaseModel):
    name: str


@router.get("/")
async def get_cards(user: dict = Depends(get_current_user)):
    p = await get_pool()
    rows = await p.fetch(
        "SELECT * FROM cards WHERE org_id=$1 ORDER BY id", user["org_id"]
    )
    return [dict(r) for r in rows]


@router.post("/")
async def create_card(c: CardIn, user: dict = Depends(get_current_user)):
    _require_card_manager(user)
    p = await get_pool()
    row = await p.fetchrow(
        "INSERT INTO cards (name, org_id) VALUES ($1,$2) RETURNING *",
        c.name,
        user["org_id"],
    )
    return dict(row)


@router.patch("/{id}")
async def update_card(id: int, c: CardIn, user: dict = Depends(get_current_user)):
    _require_card_manager(user)
    p = await get_pool()
    row = await p.fetchrow(
        "UPDATE cards SET name=$1 WHERE id=$2 AND org_id=$3 RETURNING *",
        c.name,
        id,
        user["org_id"],
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)


@router.patch("/{id}/default")
async def set_default_card(id: int, user: dict = Depends(get_current_user)):
    """Mark one card as the default within the org and clear the flag on the rest."""
    _require_card_manager(user)
    p = await get_pool()
    if not await p.fetchrow(
        "SELECT id FROM cards WHERE id=$1 AND org_id=$2", id, user["org_id"]
    ):
        raise HTTPException(status_code=404, detail="Not found")
    # Single statement keeps "exactly one default per org" atomic.
    await p.execute(
        "UPDATE cards SET is_default = (id = $1) WHERE org_id=$2", id, user["org_id"]
    )
    return {"ok": True}


@router.delete("/{id}")
async def delete_card(id: int, user: dict = Depends(get_current_user)):
    _require_card_manager(user)
    p = await get_pool()
    await p.execute("DELETE FROM cards WHERE id=$1 AND org_id=$2", id, user["org_id"])
    return {"ok": True}
