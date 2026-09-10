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

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

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


class ПрочитаноIn(BaseModel):
    """Какие события человек видел. Пусто — значит «первые ПРЕДЕЛ»."""

    model_config = ConfigDict(extra="forbid")
    ids: Optional[list[int]] = None


@router.post("/read")
async def пометить_прочитанными(
    тело: Optional[ПрочитаноIn] = None, user: dict = Depends(get_current_user)
):
    """Гасим ТОЛЬКО ТО, ЧТО ЧЕЛОВЕК ВИДЕЛ. Возвращает, сколько погасили.

    ⚠️ РАНЬШЕ ГАСИЛОСЬ ВСЁ НЕПРОЧИТАННОЕ, И ЭТО БЫЛА ЛОЖЬ В БАЗЕ (правка
    10.09.2026, решение владельца). Список отдаёт ПРЕДЕЛ = 20 строк, а
    гашение шло без всякого предела: у человека с двадцатью пятью событиями
    открытие шторки помечало прочитанными все двадцать пять, из которых пять
    на экране не побывали. **Непрочитанное значит «человек не видел»;
    пометить невыведенное — потерять уведомление навсегда**, потому что
    вернуть его нечем: точка погасла, в списке оно двадцать первое.

    ⚠️ ПОЧЕМУ ID ПРИХОДЯТ ОТ КЛИЕНТА, А НЕ СЧИТАЮТСЯ ТУТ ЖЕ ПО ТОМУ ЖЕ
    LIMIT. Второе выглядит проще и почти работает — но между GET и POST
    может лечь новое событие, и «первые двадцать» уже другие: свежее
    погасло бы, не побывав на экране. Это ровно та беда, которую чиним,
    только в миниатюре. Клиент знает, что нарисовал, — он и говорит.

    ⚠️ ЧУЖОЕ ПОГАСИТЬ НЕЛЬЗЯ: `user_id=$1` в условии остаётся при обоих
    путях, поэтому присланный чужой id просто не найдётся.

    ⚠️ ПУСТОЕ ТЕЛО — ЗАКОННЫЙ ПУТЬ, а не забывчивость клиента: гасим первые
    ПРЕДЕЛ в том же порядке, что и выдаём. Так ведёт себя старый клиент
    и любой сторонний вызов; поведение хуже точного, но не лживее прежнего.
    """
    p = await get_pool()
    ids = (тело.ids if тело else None) or None
    if ids:
        строки = await p.fetch(
            "UPDATE notifications SET read_at = NOW() "
            "WHERE user_id=$1 AND read_at IS NULL AND id = ANY($2::int[]) "
            "RETURNING id",
            user["id"],
            ids,
        )
    else:
        строки = await p.fetch(
            "UPDATE notifications SET read_at = NOW() WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, read_at FROM notifications WHERE user_id=$1"
            "    ORDER BY created_at DESC, id DESC"
            f"    LIMIT {ПРЕДЕЛ}"
            "  ) видимые WHERE read_at IS NULL"
            ") RETURNING id",
            user["id"],
        )
    return {"read": len(строки)}
