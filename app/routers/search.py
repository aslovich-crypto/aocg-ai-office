# -*- coding: utf-8 -*-
"""Общий поиск с «Главной»: чеки и отчёты разом (T144).

⚠️ РЕШЕНИЕ ВЛАДЕЛЬЦА: поиск на главном экране ищет ВСЁ СРАЗУ — «человек
не должен помнить, где что лежит». До этой ручки поиска на бэкенде не было
НИ ОДНОЙ (замер 02.09.2026: 50 путей, слово search — ноль), а «поиск» на
«Главной» был кнопкой-заглушкой, уводившей на «Чеки».

⚠️ ВИДИМОСТЬ — ТА ЖЕ, ЧТО ВЕЗДЕ (A-ACL): admin/accountant ищут по всей
организации, сотрудник — только по своим чекам и своим отчётам. Отдельный
поиск не имеет права показывать больше, чем показывают списки.

⚠️ ДВЕ СЕКЦИИ В ОТВЕТЕ, А НЕ ОДИН СПИСОК — решение владельца по виду:
у чека и отчёта разная строка, в одном списке они обрезались бы до общего
знаменателя. Пустую секцию фронт прячет.
"""

from fastapi import APIRouter, Depends

from app.auth import can_see_all, get_current_user
from app.database import get_pool

router = APIRouter(prefix="/api", tags=["search"])

# Не меньше двух знаков: по одной букве совпадает всё подряд, и выдача
# превращается в шум. Пустой ответ на короткий запрос — не ошибка.
МИНИМУМ_ЗНАКОВ = 2
ПРЕДЕЛ = 20


@router.get("/search")
async def общий_поиск(q: str = "", user: dict = Depends(get_current_user)):
    строка = q.strip()
    if len(строка) < МИНИМУМ_ЗНАКОВ:
        return {"q": строка, "receipts": [], "reports": []}
    p = await get_pool()
    шаблон = f"%{строка}%"

    # ⚠️ ILIKE, а не LIKE: человек пишет «ромашка», в чеке «ООО Ромашка».
    # Ищем по трём именам продавца: бренд, юрлицо и то, что показывается.
    if can_see_all(user["role"]):
        чеки = await p.fetch(
            "SELECT id, org, org_brand, date, amount FROM receipts "
            "WHERE org_id=$1 AND (org ILIKE $2 OR org_brand ILIKE $2 "
            "OR org_legal ILIKE $2) ORDER BY date DESC LIMIT 20",
            user["org_id"],
            шаблон,
        )
        отчёты = await p.fetch(
            "SELECT id, title, status, total, created FROM reports "
            "WHERE org_id=$1 AND title ILIKE $2 ORDER BY created DESC LIMIT 20",
            user["org_id"],
            шаблон,
        )
    else:
        чеки = await p.fetch(
            "SELECT id, org, org_brand, date, amount FROM receipts "
            "WHERE org_id=$1 AND user_id=$3 AND (org ILIKE $2 OR org_brand ILIKE $2 "
            "OR org_legal ILIKE $2) ORDER BY date DESC LIMIT 20",
            user["org_id"],
            шаблон,
            user["id"],
        )
        отчёты = await p.fetch(
            "SELECT id, title, status, total, created FROM reports "
            "WHERE org_id=$1 AND user_id=$3 AND title ILIKE $2 "
            "ORDER BY created DESC LIMIT 20",
            user["org_id"],
            шаблон,
            user["id"],
        )

    return {
        "q": строка,
        "receipts": [
            {
                "id": r["id"],
                "org": r["org"],
                "org_brand": r["org_brand"],
                "date": r["date"].isoformat() if r["date"] else None,
                "amount": float(r["amount"]) if r["amount"] is not None else None,
            }
            for r in чеки
        ],
        "reports": [
            {
                "id": r["id"],
                "title": r["title"],
                "status": r["status"],
                "total": float(r["total"]) if r["total"] is not None else None,
                "created": r["created"].isoformat() if r["created"] else None,
            }
            for r in отчёты
        ],
    }
