from __future__ import annotations

from typing import Any

from db_ops.telegram.api import DEFAULT_TELEGRAM_API_URL, call_telegram_api, get_updates, send_message
from db_ops.telegram.client import TelegramClient
from db_ops.telegram.commands import DEFAULT_COMMAND_PREFIX, can_run_command, save_command_messages_from_messages
from db_ops.telegram.updates import fetch_and_save_updates, save_updates


__all__ = [
    "DEFAULT_COMMAND_PREFIX",
    "DEFAULT_TELEGRAM_API_URL",
    "TelegramClient",
    "call_telegram_api",
    "can_run_command",
    "fetch_and_save_updates",
    "get_updates",
    "process_pending_command_messages",
    "process_one_command_message",
    "run_bot_workflow",
    "save_command_messages_from_messages",
    "save_updates",
    "send_message",
    "send_one_message",
    "send_pending_messages",
]


def __getattr__(name: str) -> Any:
    if name in {"process_pending_command_messages", "process_one_command_message"}:
        from db_ops.telegram import command_processor

        return getattr(command_processor, name)
    if name in {"send_one_message", "send_pending_messages"}:
        from db_ops.telegram import send_queue

        return getattr(send_queue, name)
    if name == "run_bot_workflow":
        from db_ops.telegram import workflow

        return workflow.run_bot_workflow
    raise AttributeError(name)
