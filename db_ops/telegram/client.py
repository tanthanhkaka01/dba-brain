from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from db_ops.telegram.api import DEFAULT_TELEGRAM_API_URL, get_updates, send_message


@dataclass(frozen=True)
class TelegramClient:
    bot_token: str
    api_url: str = DEFAULT_TELEGRAM_API_URL
    timeout_seconds: int = 20

    def send_message(self, *, chat_id: str, text: str) -> dict[str, Any]:
        return send_message(
            bot_token=self.bot_token,
            chat_id=chat_id,
            text=text,
            api_url=self.api_url,
            timeout_seconds=self.timeout_seconds,
        )

    def get_updates(
        self,
        *,
        offset: int | None = None,
        limit: int | None = None,
        allowed_updates: list[str] | None = None,
    ) -> dict[str, Any]:
        return get_updates(
            bot_token=self.bot_token,
            api_url=self.api_url,
            timeout_seconds=self.timeout_seconds,
            offset=offset,
            limit=limit,
            allowed_updates=allowed_updates,
        )
