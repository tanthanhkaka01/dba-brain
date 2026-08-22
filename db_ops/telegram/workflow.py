from __future__ import annotations

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
    conversation_result = process_pending_conversation_messages(
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
