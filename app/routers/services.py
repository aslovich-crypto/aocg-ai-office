"""Integration status for the Settings → Сервисы tab.

The frontend can't read the server's environment, so the only dynamic bit —
whether ANTHROPIC_API_KEY is configured — has to come from the backend. The
two other integrations are static placeholders for now.

Status values: active | in_progress | not_connected | not_configured
"""

import os

from fastapi import APIRouter

router = APIRouter(prefix="/api/services", tags=["services"])


@router.get("/")
async def get_services():
    anthropic_ready = bool(os.getenv("ANTHROPIC_API_KEY"))
    return [
        {
            "key": "fns",
            "name": "ФНС «Мои чеки онлайн»",
            "status": "not_connected",
            "description": "Автоматическая загрузка чеков из личного кабинета ФНС",
        },
        {
            "key": "alfabank",
            "name": "Альфа-Банк API",
            "status": "in_progress",
            "description": "Импорт банковских операций по счёту",
        },
        {
            "key": "anthropic",
            "name": "Anthropic OCR",
            "status": "active" if anthropic_ready else "not_configured",
            "description": "Распознавание чеков по фото (Claude Vision)",
        },
    ]
