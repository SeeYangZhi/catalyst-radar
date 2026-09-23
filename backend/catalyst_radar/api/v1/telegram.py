import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from catalyst_radar.api.deps import CurrentUser, SessionDep
from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger
from catalyst_radar.repositories.notification_repository import (
    TelegramChatRepository,
)
from catalyst_radar.services.telegram_bot import (
    BOT_COMMANDS,
    deliver_action,
    process_update,
)
from catalyst_radar.services.telegram_client import (
    TelegramClient,
    TelegramConfigError,
)

router = APIRouter(prefix="/telegram", tags=["telegram"])
log = get_logger(__name__)


class TestMessage(BaseModel):
    chat_id: str
    text: str = "Catalyst Radar test alert."


@router.post("/webhook")
async def telegram_webhook(
    update: dict[str, Any],
    session: SessionDep,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Public endpoint for Telegram update delivery.

    When TELEGRAM_WEBHOOK_SECRET is configured, Telegram echoes it in the
    X-Telegram-Bot-Api-Secret-Token header; any request without the exact
    match is rejected so the public endpoint cannot be spoofed. The compare
    is constant-time (``hmac.compare_digest``) so the secret can't be
    recovered by timing the 403. (Telegram's Bot API only offers this echoed
    shared secret — there is no request-body HMAC signature to verify.)
    """
    secret = settings.telegram_webhook_secret
    if secret and not hmac.compare_digest(x_telegram_bot_api_secret_token or "", secret):
        log.warning("telegram_webhook_bad_secret")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid secret token")

    action = await process_update(session, update)
    if action is None:
        return {"status": "ignored"}

    client = TelegramClient()
    if client.configured and action.chat_id:
        await deliver_action(client, action)
    return {"status": "ok"}


@router.post("/webhook/register")
async def telegram_webhook_register(current_user: CurrentUser) -> dict[str, Any]:
    """(Re)register the webhook from TELEGRAM_WEBHOOK_URL without a redeploy."""
    if not settings.telegram_webhook_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TELEGRAM_WEBHOOK_URL is not configured",
        )
    client = TelegramClient()
    if not client.configured:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TELEGRAM_BOT_TOKEN is not configured",
        )
    ok = await client.set_webhook(settings.telegram_webhook_url, settings.telegram_webhook_secret)
    await client.set_commands(BOT_COMMANDS)
    return {"ok": ok, "url": settings.telegram_webhook_url}


@router.delete("/webhook")
async def telegram_webhook_delete(current_user: CurrentUser) -> dict[str, Any]:
    """Remove the webhook (reverts to getUpdates polling)."""
    client = TelegramClient()
    if not client.configured:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TELEGRAM_BOT_TOKEN is not configured",
        )
    return {"ok": await client.delete_webhook()}


@router.get("/status")
async def telegram_status(current_user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    chats = await TelegramChatRepository(session).recipients()
    return {
        "configured": TelegramClient().configured,
        "registered_chats": [c.chat_id for c in chats],
    }


@router.post("/test")
async def telegram_test(
    payload: TestMessage,
    current_user: CurrentUser,
) -> dict[str, Any]:
    client = TelegramClient()
    try:
        result = await client.send_message(payload.chat_id, payload.text)
    except TelegramConfigError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"ok": result.ok, "description": result.description}
