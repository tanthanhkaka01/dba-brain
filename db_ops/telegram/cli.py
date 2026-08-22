from __future__ import annotations

import argparse
import inspect
import json
import sys
from collections.abc import Callable
from typing import Any
from pathlib import Path

from db_ops.lib.secret_text import add_key_argument, set_key_env
from db_ops.config import (
    DEFAULT_CONFIG_PATH,
    DbOpsConfig,
    load_config,
    resolve_config_path,
    resolve_telegram_config_path,
)
from db_ops.logging_ops import log_function_call, log_function_error, setup_app_logger
from db_ops.telegram.command_processor import (
    process_one_command_message,
    process_pending_command_messages,
    process_pending_conversation_messages,
)
from db_ops.telegram.commands import save_command_messages_from_messages
from db_ops.telegram.metrics_reports import queue_metrics_reports
from db_ops.telegram.send_queue import send_one_message, send_pending_messages
from db_ops.telegram import get_updates, send_message
from db_ops.telegram.updates import fetch_and_save_updates
from db_ops.telegram.workflow import run_bot_workflow
from db_ops.logging_ops.runtime_stdout import patch_stdout


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Telegram Bot API function.")
    parser.add_argument("--config", default=None, help="Path to config JSON. Defaults to config.telegram.json or config.json.")
    add_key_argument(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    send_parser = subparsers.add_parser("send-message", help="Call Telegram sendMessage.")
    send_parser.add_argument("--chat-id", required=True, help="Telegram chat id.")
    send_parser.add_argument("--text", required=True, help="Message text.")
    send_parser.set_defaults(telegram_function=send_message)

    updates_parser = subparsers.add_parser("get-updates", help="Call Telegram getUpdates.")
    updates_parser.add_argument("--offset", type=int, default=None, help="Optional update offset.")
    updates_parser.add_argument("--limit", type=int, default=None, help="Optional update limit.")
    updates_parser.add_argument(
        "--allowed-update",
        action="append",
        dest="allowed_updates",
        default=None,
        help="Allowed update type. Can be passed multiple times.",
    )
    updates_parser.set_defaults(telegram_function=get_updates)

    save_updates_parser = subparsers.add_parser("save-updates", help="Call getUpdates and save messages, groups, users.")
    save_updates_parser.add_argument("--offset", type=int, default=None, help="Optional update offset.")
    save_updates_parser.add_argument("--limit", type=int, default=None, help="Optional update limit.")
    save_updates_parser.add_argument(
        "--allowed-update",
        action="append",
        dest="allowed_updates",
        default=None,
        help="Allowed update type. Can be passed multiple times.",
    )
    save_updates_parser.add_argument("--data-dir", default="data", help="Directory for telegram_messages/groups/users JSON.")
    save_updates_parser.set_defaults(telegram_function=fetch_and_save_updates)

    save_commands_parser = subparsers.add_parser("save-commands", help="Copy Telegram messages with command prefix to command table.")
    save_commands_parser.add_argument("--command-prefix", default="/spbot", help="Command prefix. Default: /spbot.")
    save_commands_parser.set_defaults(telegram_function=save_command_messages_from_messages)

    process_commands_parser = subparsers.add_parser("process-commands", help="Process pending Telegram command messages.")
    process_commands_parser.add_argument("--commands-path", default="data/telegram_support_commands.json", help="Path to support command JSON.")
    process_commands_parser.add_argument("--limit", type=int, default=50, help="Maximum pending command messages to process.")
    process_commands_parser.set_defaults(telegram_function=process_pending_command_messages)

    process_one_command_parser = subparsers.add_parser("process-one-command", help="Process one Telegram command message row.")
    process_one_command_parser.add_argument("--telegram-command-message-id", type=int, required=True, help="telegram_command_messages.telegram_command_message_id.")
    process_one_command_parser.add_argument("--commands-path", default="data/telegram_support_commands.json", help="Path to support command JSON.")
    process_one_command_parser.set_defaults(telegram_function=process_one_command_message)

    process_conversations_parser = subparsers.add_parser("process-conversations", help="Process pending Telegram conversation states.")
    process_conversations_parser.add_argument("--commands-path", default="data/telegram_support_commands.json", help="Path to support command JSON.")
    process_conversations_parser.add_argument("--limit", type=int, default=50, help="Maximum waiting conversation states to process.")
    process_conversations_parser.set_defaults(telegram_function=process_pending_conversation_messages)

    send_queue_parser = subparsers.add_parser("send-queue", help="Send pending rows from telegram_send_messages.")
    send_queue_parser.add_argument("--limit", type=int, default=50, help="Maximum pending messages to send.")
    send_queue_parser.add_argument("--retry-count", type=int, default=3, help="Retry count per message. Default: 3.")
    send_queue_parser.set_defaults(telegram_function=send_pending_messages)

    send_one_parser = subparsers.add_parser("send-one", help="Send one row from telegram_send_messages by send_tlgmsg_id.")
    send_one_parser.add_argument("--send-tlgmsg-id", type=int, required=True, help="telegram_send_messages.send_tlgmsg_id.")
    send_one_parser.add_argument("--retry-count", type=int, default=3, help="Retry count. Default: 3.")
    send_one_parser.set_defaults(telegram_function=send_one_message)

    metrics_parser = subparsers.add_parser("queue-metrics-reports", help="Compatibility workflow for reports queue-metrics-reports.")
    metrics_parser.add_argument("--summary-limit", type=int, default=40, help="Maximum alert detail lines per metrics report.")
    metrics_parser.add_argument("--target-id", help="Only queue metrics reports for one target_id.")
    metrics_parser.add_argument("--dedupe-seconds", type=int, default=300, help="Do not queue the same metrics report level again within this many seconds.")
    metrics_parser.set_defaults(telegram_function=queue_metrics_reports)

    # Routing lookups, for the apps that need to know where an alert goes. They answer from
    # config alone - no bot call, no store - so they return before main() sets up logging and
    # patches stdout: an app parses this JSON, and a log line mixed into it would break it.
    route_parser = subparsers.add_parser("route", help="Print {enabled, alert, chat_id} as JSON for a notify level.")
    route_parser.add_argument("level", help="Notify level: logging | warning | error | critical | a configured level.")
    route_parser.set_defaults(telegram_function=None)

    groups_parser = subparsers.add_parser("groups", help="Print the configured level -> chat_id map as JSON.")
    groups_parser.set_defaults(telegram_function=None)

    workflow_parser = subparsers.add_parser("run-workflow", help="Run getUpdates, save messages, process commands, and send replies.")
    workflow_parser.add_argument("--offset", type=int, default=None, help="Optional update offset.")
    workflow_parser.add_argument("--limit", type=int, default=20, help="getUpdates limit.")
    workflow_parser.add_argument("--allowed-update", action="append", dest="allowed_updates", default=None, help="Allowed update type.")
    workflow_parser.add_argument("--data-dir", default="data", help="Data directory for groups/users JSON.")
    workflow_parser.add_argument("--command-prefix", default="/spbot", help="Command prefix. Default: /spbot.")
    workflow_parser.add_argument("--commands-path", default="data/telegram_support_commands.json", help="Path to support command JSON.")
    workflow_parser.add_argument("--command-limit", type=int, default=50, help="Maximum pending commands to process.")
    workflow_parser.add_argument("--send-limit", type=int, default=50, help="Maximum pending send messages to send.")
    workflow_parser.add_argument("--retry-count", type=int, default=3, help="Retry count per send message.")
    workflow_parser.set_defaults(telegram_function=run_bot_workflow)

    return parser.parse_args(argv)


def call_telegram_function(
    *,
    telegram_function: Callable[..., dict[str, Any]],
    args: argparse.Namespace,
    config: DbOpsConfig,
    config_path: str,
) -> dict[str, Any]:
    available_values = vars(args) | {
        "config_path": config_path,
        "bot_token": config.telegram.resolved_bot_token,
        "api_url": config.telegram.api_url,
        "timeout_seconds": config.telegram.timeout_seconds,
        # The store declaration, not a path: helpers pass this straight into a store
        # class, so it must follow data/store_config.json rather than pinning SQLite.
        "sqlite_path": config.store,
        "telegram_groups": config.telegram.level_chat_map,
    }
    function_params = inspect.signature(telegram_function).parameters
    function_args = {
        name: available_values[name]
        for name in function_params
        if name in available_values
    }
    return telegram_function(**function_args)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    set_key_env(args.key, args.key_base64)

    # Before any logging setup or stdout patching: these two read config, print one JSON object
    # and exit. They are called by other apps on the hot path of every notification, so they must
    # be cheap and their stdout must carry nothing but the answer.
    if args.command in ("route", "groups"):
        from db_ops.telegram import routing

        try:
            answer = routing.route_for_level(args.level) if args.command == "route" else routing.groups()
        except Exception as exc:  # noqa: BLE001 - a routing failure must be reported, not guessed at.
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(answer, ensure_ascii=False))
        return 0

    logger = None
    try:
        config_path = str(resolve_config_path("telegram", args.config))
        config = load_config(config_path)
        patch_stdout(config.log_dir / "telegram_runtime.log", app_name="telegram")
        logger = setup_app_logger(config, app_name="telegram", enable_telegram_alerts=False, enable_console=False)
        if args.command in ("save-updates", "run-workflow") and args.offset is None:
            args.offset = config.telegram.update_offset
        log_function_call(logger, function_name=f"telegram.{args.command}", text=getattr(args, "text", ""))
        result = call_telegram_function(telegram_function=args.telegram_function, args=args, config=config, config_path=config_path)
        if args.command in ("save-updates", "run-workflow"):
            save_next_update_offset(config_path, result.get("next_update_offset"))
    except Exception as exc:  # noqa: BLE001 - command-line failure path.
        if logger:
            log_function_error(logger, function_name=f"telegram.{args.command}", error_text=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def save_next_update_offset(config_path: str, next_update_offset: Any) -> None:
    if next_update_offset is None:
        return

    # Telegram settings (incl. update_offset) live in data/telegram_config.json.
    path = resolve_telegram_config_path(config_path)
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)

    if data.get("update_offset") == next_update_offset:
        return

    data["update_offset"] = next_update_offset
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
