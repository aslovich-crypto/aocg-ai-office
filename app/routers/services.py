"""Integration status for the Settings → Сервисы tab.

The frontend can't read the server's environment, so the only dynamic bit —
whether ANTHROPIC_API_KEY is configured — has to come from the backend. The
two other integrations are static placeholders for now.

Status values: active | in_progress | not_connected | not_configured
"""

import os

from fastapi import APIRouter, Depends

from app.auth import get_current_user

router = APIRouter(prefix="/api/services", tags=["services"])


@router.get("/")
async def get_services(user: dict = Depends(get_current_user)):
    # S-32: до 07.08.2026 ручка была единственной без проверки авторизации —
    # любой прохожий видел состав интеграций и то, настроен ли ключ OCR.
    # ПД тут нет, но наружу светило внутреннее состояние конфигурации.
    # Роль не проверяется намеренно: вкладка «Сервисы» доступна всем своим.
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
