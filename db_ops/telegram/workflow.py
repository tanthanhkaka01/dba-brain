from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from db_ops.telegram.command_processor import check_cli_background_tasks, process_pending_command_messages, process_pending_conversation_messages
from db_ops.telegram.commands import save_command_messages_from_messages
from db_ops.telegram.send_queue import send_pending_messages
from db_ops.telegram.updates import DEFAULT_DATA_DIR, fetch_and_save_updates


def run_bot_workflow(
    *,
    sqlite_path: str | Path,
    bot_token: str,
    api_url: str = "https://api.telegram.org",
    timeout_seconds: int = 20,
    offset: int | None = None,
    limit: int = 20,
    allowed_updates: list[str] | None = None,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    command_prefix: str = "/spbot",
    commands_path: str | Path = DEFAULT_DATA_DIR / "telegram_support_commands.json",
    config_path: str | Path = "config.json",
    command_limit: int = 50,
    send_limit: int = 50,
    retry_count: int = 3,
    send_per_chat: int = 5,
    send_threads: int = 10,
    pauses_path: str | Path | None = None,
    budget_seconds: float | None = None,
    budget_started_at: float | None = None,
    save_offset: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    updates_result = fetch_and_save_updates(
        bot_token=bot_token,
        api_url=api_url,
        timeout_seconds=timeout_seconds,
        offset=offset,
        limit=limit,
        allowed_updates=allowed_updates,
        data_dir=data_dir,
        sqlite_path=sqlite_path,
    )
    if save_offset is not None:
        # Now, not when the workflow returns. The messages are in the store, so Telegram need not
        # send them again; waiting for the end meant any later step that raised - or the daemon
        # killing the run at its timeout - kept the old offset, and every run re-read the same
        # `limit` updates while the ones behind them were never fetched: a bot that stopped hearing.
        save_offset(updates_result.get("next_update_offset"))
    command_messages_result = save_command_messages_from_messages(
        sqlite_path=sqlite_path,
        command_prefix=command_prefix,
    )
    process_result = process_pending_command_messages(
        sqlite_path=sqlite_path,
        commands_path=commands_path,
        config_path=config_path,
        limit=command_limit,
    )
    def delete_message(chat_id: str, message_id: int) -> None:
        from db_ops.telegram.api import call_telegram_api

        call_telegram_api(bot_token=bot_token, method_name="deleteMessage",
                          payload={"chat_id": chat_id, "message_id": message_id}, api_url=api_url,
                          timeout_seconds=10)

    conversation_result = process_pending_conversation_messages(
        delete_message=delete_message,
        sqlite_path=sqlite_path,
        commands_path=commands_path,
        config_path=config_path,
        limit=command_limit,
    )
    background_result = check_cli_background_tasks(sqlite_path=sqlite_path)
    send_result = send_pending_messages(
        sqlite_path=sqlite_path,
        bot_token=bot_token,
        api_url=api_url,
        timeout_seconds=timeout_seconds,
        limit=send_limit,
        retry_count=retry_count,
        send_per_chat=send_per_chat,
        send_threads=send_threads,
        pauses_path=pauses_path,
        budget_seconds=budget_seconds,
        budget_started_at=budget_started_at,
    )
    return {
        "ok": True,
        "next_update_offset": updates_result.get("next_update_offset"),
        "steps": {
            "get_updates_insert_messages": updates_result,
            "insert_command_messages": command_messages_result,
            "insert_send_messages_from_command_messages": process_result,
            "process_conversation_messages": conversation_result,
            "check_cli_background_tasks": background_result,
            "send_messages": send_result,
        },
    }
