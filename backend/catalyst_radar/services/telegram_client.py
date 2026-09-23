import asyncio
from dataclasses import dataclass

import httpx

from catalyst_radar.config import settings
from catalyst_radar.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class SendResult:
    ok: bool
    message_id: int | None = None
    error_code: int | None = None
    description: str | None = None


class TelegramConfigError(RuntimeError):
    """Raised when the Telegram bot token is not configured."""


class TelegramClient:
    """Thin async Telegram Bot API client with bounded retry/backoff."""

    def __init__(self, token: str | None = None, parse_mode: str | None = None) -> None:
        self.token = token if token is not None else settings.telegram_bot_token
        self.parse_mode = parse_mode or settings.telegram_parse_mode
        self.timeout = settings.telegram_request_timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _url(self, method: str) -> str:
        if not self.token:
            raise TelegramConfigError("TELEGRAM_BOT_TOKEN is not configured")
        return f"https://api.telegram.org/bot{self.token}/{method}"

    async def get_updates(self, offset: int | None = None, poll_timeout: int = 0) -> list[dict]:
        """Long-poll updates. Returns the raw update list (empty on error).

        ``poll_timeout`` is Telegram's getUpdates long-poll seconds.
        """
        url = self._url("getUpdates")
        params: dict[str, int] = {"timeout": poll_timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            async with httpx.AsyncClient(timeout=self.timeout + poll_timeout) as client:
                resp = await client.get(url, params=params)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("telegram_get_updates_failed", error=repr(exc))
            return []
        if not data.get("ok"):
            log.warning("telegram_get_updates_error", description=data.get("description"))
            return []
        return [u for u in data.get("result", []) if isinstance(u, dict)]

    async def set_webhook(self, url: str, secret: str | None = None) -> bool:
        """Register the webhook. Telegram will echo ``secret`` in the
        X-Telegram-Bot-Api-Secret-Token header on every delivery."""
        body: dict[str, object] = {"url": url, "drop_pending_updates": False}
        if secret:
            body["secret_token"] = secret
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self._url("setWebhook"), json=body)
            ok = bool(resp.json().get("ok"))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("telegram_set_webhook_failed", error=repr(exc))
            return False
        log.info("telegram_set_webhook", url=url, ok=ok)
        return ok

    async def delete_webhook(self) -> bool:
        """Remove the webhook (returns to getUpdates polling)."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self._url("deleteWebhook"), json={})
            ok = bool(resp.json().get("ok"))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("telegram_delete_webhook_failed", error=repr(exc))
            return False
        log.info("telegram_delete_webhook", ok=ok)
        return ok

    async def set_commands(self, commands: list[dict[str, str]]) -> bool:
        """Register the bot command menu (shown when user types '/')."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self._url("setMyCommands"), json={"commands": commands}
                )
            ok = bool(resp.json().get("ok"))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("telegram_set_commands_failed", error=repr(exc))
            return False
        log.info("telegram_set_commands", count=len(commands), ok=ok)
        return ok

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict | None = None,
        max_attempts: int = 3,
    ) -> SendResult:
        url = self._url("sendMessage")
        body: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": self.parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        last: SendResult = SendResult(ok=False, description="not attempted")
        for attempt in range(1, max_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=body)
                data = resp.json()
                if data.get("ok"):
                    result = data.get("result", {})
                    return SendResult(ok=True, message_id=result.get("message_id"))
                last = SendResult(
                    ok=False,
                    error_code=data.get("error_code"),
                    description=data.get("description"),
                )
                # 4xx (bad request, blocked) are not retryable.
                if resp.status_code < 500 and data.get("error_code") != 429:
                    return last
            except (httpx.HTTPError, ValueError) as exc:
                last = SendResult(ok=False, description=repr(exc))
            if attempt < max_attempts:
                await asyncio.sleep(min(2**attempt, 10))
        log.warning("telegram_send_failed", chat_id=chat_id, error=last.description)
        return last

    async def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> SendResult:
        """Edit an existing message in place (used for tap-through navigation
        so the chat is not flooded with new messages)."""
        body: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": self.parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self._url("editMessageText"), json=body)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return SendResult(ok=False, description=repr(exc))
        if data.get("ok"):
            return SendResult(ok=True, message_id=message_id)
        # "message is not modified" is benign (identical re-render on re-tap).
        desc = str(data.get("description") or "")
        if "not modified" in desc:
            return SendResult(ok=True, message_id=message_id)
        log.warning("telegram_edit_failed", chat_id=chat_id, error=desc)
        return SendResult(ok=False, error_code=data.get("error_code"), description=desc)

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> bool:
        """Acknowledge a button tap so Telegram stops the client spinner.
        Optional ``text`` shows a brief toast to the user."""
        body: dict[str, object] = {"callback_query_id": callback_query_id}
        if text:
            body["text"] = text
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self._url("answerCallbackQuery"), json=body)
            return bool(resp.json().get("ok"))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("telegram_answer_callback_failed", error=repr(exc))
            return False
