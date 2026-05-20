from fastapi import APIRouter, HTTPException
from app.database import get_pool
from pydantic import BaseModel

router = APIRouter(prefix="/api/cards", tags=["cards"])

class CardIn(BaseModel):
    name: str

@router.get("/")
async def get_cards():
    p = await get_pool()
    rows = await p.fetch("SELECT * FROM cards ORDER BY id")
    return [dict(r) for r in rows]

@router.post("/")
async def create_card(c: CardIn):
    p = await get_pool()
    row = await p.fetchrow("INSERT INTO cards (name) VALUES ($1) RETURNING *", c.name)
    return dict(row)

@router.patch("/{id}")
async def update_card(id: int, c: CardIn):
    p = await get_pool()
    row = await p.fetchrow("UPDATE cards SET name=$1 WHERE id=$2 RETURNING *", c.name, id)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return dict(row)

@router.patch("/{id}/default")
async def set_default_card(id: int):
    """Mark one card as the default and clear the flag on all others, so the
    scanner can auto-fill it when a card receipt has no per-org history."""
    p = await get_pool()
    if not await p.fetchrow("SELECT id FROM cards WHERE id=$1", id):
        raise HTTPException(status_code=404, detail="Not found")
    # Single statement keeps "exactly one default" atomic.
    await p.execute("UPDATE cards SET is_default = (id = $1)", id)
    return {"ok": True}

@router.delete("/{id}")
async def delete_card(id: int):
    p = await get_pool()
    await p.execute("DELETE FROM cards WHERE id=$1", id)
    return {"ok": True}
