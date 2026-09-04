# -*- coding: utf-8 -*-
"""Список событий и счётчик непрочитанных для колокольчика (T159).

⚠️ ЧУЖИХ СОБЫТИЙ НЕ БЫВАЕТ. Строка события принадлежит человеку — адресат
записан в ней в момент создания. Поэтому здесь нет ни роли, ни org-условия
сверх `user_id`: смотреть чужие уведомления нельзя НИКОМУ, включая
администратора. Это не про доступ к данным, а про то, что уведомление —
личная почта, а не общий журнал.

⚠️ ПРОЧИТАННОСТЬ — ОТКРЫТИЕМ СПИСКА, решение владельца 04.09.2026.
Колокольчик отвечает на один вопрос: «есть ли новое». При поштучной
пометке точка горела бы, когда смотреть уже нечего, — и человек перестал
бы ей верить.
"""

from fastapi import APIRouter, Depends

from app.auth import get_current_user
from app.database import get_pool

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

# Сколько событий отдаём разом. Колокольчик — это «что нового», а не архив:
# двадцати строк хватает на несколько дней, а листание списка мы не рисовали.
ПРЕДЕЛ = 20


@router.get("/")
async def список(user: dict = Depends(get_current_user)):
    """Мои события, новые сверху, и число непрочитанных для точки."""
    p = await get_pool()
    строки = await p.fetch(
        "SELECT id, kind, title, body, report_id, created_at, read_at "
        "FROM notifications WHERE user_id=$1 ORDER BY created_at DESC, id DESC "
        f"LIMIT {ПРЕДЕЛ}",
        user["id"],
    )
    непрочитано = await p.fetchval(
        "SELECT count(*) FROM notifications WHERE user_id=$1 AND read_at IS NULL",
        user["id"],
    )
    return {
        # ⚠️ СЧЁТЧИК СЧИТАЕТСЯ ПО ВСЕЙ ТАБЛИЦЕ, А НЕ ПО ВЫДАННЫМ ДВАДЦАТИ:
        # иначе двадцать первое непрочитанное не зажгло бы точку, и человек
        # не узнал бы о событии вовсе.
        "unread": непрочитано or 0,
        "items": [
            {
                "id": с["id"],
                "kind": с["kind"],
                "title": с["title"],
                "body": с["body"],
                "report_id": с["report_id"],
                "created_at": с["created_at"].isoformat() if с["created_at"] else None,
                "read": с["read_at"] is not None,
            }
            for с in строки
        ],
    }


@router.post("/read")
async def пометить_прочитанными(user: dict = Depends(get_current_user)):
    """Открыл список — значит увидел всё. Возвращает, сколько погасили."""
    p = await get_pool()
    строки = await p.fetch(
        "UPDATE notifications SET read_at = NOW() "
        "WHERE user_id=$1 AND read_at IS NULL RETURNING id",
        user["id"],
    )
    return {"read": len(строки)}
