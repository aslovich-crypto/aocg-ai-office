#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ручки модуля «Бюджет проекта». FIN-01 — только чтение справочника CoA.

    GET /api/finance/ping    — маркер сборки, БЕЗ авторизации
    GET /api/finance/coa/    — статьи справочника организации пользователя

⚠️ `ping` ОТКРЫТ ОСОЗНАННО, И ЭТО НАЗВАНО ВСЛУХ, А НЕ СДЕЛАНО ТИХО. Он нужен,
чтобы проверить снаружи, доехала ли сборка до прода; данных не отдаёт вовсе —
только имя модуля и маркер версии. Почему это пишется здесь: 07.08.2026 в отчёт
ушло «единственная ручка без авторизации», а замер по коду через сутки дал
ДВЕНАДЦАТЬ открытых ручек, четыре из них закрывать было нужно немедленно.
Открытая ручка, о которой не сказано, становится такой же незаметной.

org-scope: `org_id` берётся ТОЛЬКО из `get_current_user`, никогда из запроса.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import get_current_user
from app.database import get_pool
from app.finance.coa_seed import ВЕРСИЯ

router = APIRouter(prefix="/api/finance", tags=["finance"])

# Маркер сборки: по нему проверяется, что до прода доехал именно этот код.
# Проверяется строкой, а не хешем бандла, — правило репозитория о верификации
# выкатки. Меняется при каждой задаче, которая меняет контракт ручек Финансов.
МАРКЕР = "FIN01-COA-v312"


@router.get("/ping")
async def ping():
    """Маркер сборки. Без авторизации — см. предупреждение в шапке модуля."""
    return {"module": "finance", "marker": МАРКЕР}


@router.get("/coa/")
async def список_статей(user: dict = Depends(get_current_user)):
    """Справочник статей организации пользователя, в порядке справочника.

    Деактивированные статьи не отдаются: `is_active=FALSE` означает «статьёй
    больше не пользуются», а не «строку удалили» — удалять нельзя, на статьи
    ссылаются бюджеты.
    """
    p = await get_pool()
    строки = await p.fetch(
        """SELECT id, code, name_ru, name_en, section, kind,
                  is_cash, is_group, is_reserve, note, position
           FROM fin_coa_articles
           WHERE org_id=$1 AND is_active=TRUE
           ORDER BY position, id""",
        user["org_id"],
    )
    return {
        "coa_version": ВЕРСИЯ,
        "count": len(строки),
        "articles": [dict(с) for с in строки],
    }
