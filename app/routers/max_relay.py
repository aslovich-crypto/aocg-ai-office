"""
Шлюз в мессенджер MAX. Путь в проекте: app/routers/max_relay.py.

Зачем это здесь, а не отдельным сервисом: MAX Bot API недоступен из-за
рубежа (Google Apps Script, Railway) — соединение рвётся на TLS. Наш
бэкенд уже стоит на Timeweb на российском IP и уже имеет рабочий
сертификат на api.aocgai.ru, так что проще дать ему два вызова наружу,
чем поднимать отдельный сервер.

НАМЕРЕННО без обычной авторизации пользователей (Bearer JWT из auth.py):
этот роутер дёргает не залогиненный сотрудник, а внешний сервис (Google
Apps Script — сводка по таблице задач; в будущем — сторож доступности и
утренние сводки). У него нет и не должно быть пользовательской сессии.
Вместо JWT — отдельный статический токен MAX_RELAY_TOKEN в заголовке
X-Relay-Token. Если понадобится авторизовать через общую систему —
это отдельное решение, а не «дописать Depends(get_current_user)».

Переменные окружения (Timeweb → AOCG-AI-001_app_prod → Переменные):
    MAX_RELAY_TOKEN   — пароль для этих двух эндпоинтов,
                        сгенерировать: openssl rand -hex 32
    MAX_TOKEN         — токен бота, из business.max.ru
    MAX_CHAT_ID       — id группового чата (можно оставить пустым,
                        тогда передавать chat_id в каждом запросе)
    MAX_API           — необязательно, по умолчанию https://platform-api2.max.ru

Эндпоинты после деплоя:
    GET  https://api.aocgai.ru/internal/max/updates
    POST https://api.aocgai.ru/internal/max/send
"""

import os
from typing import Optional

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/internal/max", tags=["max-relay"])

MAX_RELAY_TOKEN = os.environ.get("MAX_RELAY_TOKEN", "")
MAX_TOKEN = os.environ.get("MAX_TOKEN", "")
MAX_CHAT_ID = os.environ.get("MAX_CHAT_ID", "")
MAX_API = os.environ.get("MAX_API", "https://platform-api2.max.ru").rstrip("/")


def _check_auth(token: Optional[str]) -> None:
    if not MAX_RELAY_TOKEN:
        raise HTTPException(500, "MAX_RELAY_TOKEN не задан в переменных окружения")
    if token != MAX_RELAY_TOKEN:
        raise HTTPException(401, "Неверный токен")


def _max_headers() -> dict:
    if not MAX_TOKEN:
        raise HTTPException(500, "MAX_TOKEN не задан в переменных окружения")
    return {"Authorization": MAX_TOKEN, "Content-Type": "application/json"}


class SendRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    chat_id: Optional[str] = None
    format: Optional[str] = "markdown"
    notify: bool = True


@router.get("/updates")
async def updates(
    limit: int = 100,
    timeout: int = 0,
    x_relay_token: Optional[str] = Header(None),
):
    """События бота — нужен один раз, чтобы узнать chat_id группового чата."""
    _check_auth(x_relay_token)

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            r = await client.get(
                f"{MAX_API}/updates",
                params={"limit": limit, "timeout": timeout},
                headers=_max_headers(),
            )
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Не достучались до MAX: {e}")

    if r.status_code >= 300:
        raise HTTPException(502, f"MAX вернул {r.status_code}: {r.text[:500]}")

    data = r.json()
    chats = {}
    for u in data.get("updates", []):
        cid = (
            u.get("chat_id")
            or (u.get("message") or {}).get("recipient", {}).get("chat_id")
            or (u.get("chat") or {}).get("chat_id")
        )
        if cid is None:
            continue
        chat = u.get("chat") or {}
        chats[str(cid)] = (
            chat.get("title")
            or chat.get("name")
            or u.get("chat_type")
            or "без названия"
        )

    return {"chats": chats, "raw_count": len(data.get("updates", []))}


@router.post("/send")
async def send(req: SendRequest, x_relay_token: Optional[str] = Header(None)):
    """Отправить текст в чат MAX."""
    _check_auth(x_relay_token)

    chat_id = req.chat_id or MAX_CHAT_ID
    if not chat_id:
        raise HTTPException(400, "chat_id не передан и MAX_CHAT_ID не задан")

    payload = {"text": req.text, "notify": req.notify}
    if req.format:
        payload["format"] = req.format

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(
                f"{MAX_API}/messages",
                params={"chat_id": chat_id},
                headers=_max_headers(),
                json=payload,
            )
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Не достучались до MAX: {e}")

    if r.status_code >= 300:
        raise HTTPException(502, f"MAX вернул {r.status_code}: {r.text[:500]}")

    return {"ok": True, "chat_id": chat_id, "max_response": r.json()}
