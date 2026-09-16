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
from db_ops.telegram import bot_info, get_updates, send_message
from db_ops.telegram.updates import set_group_level, set_user_level
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

    info_parser = subparsers.add_parser(
        "bot-info",
        help="Who this token belongs to (id, username) and whether privacy mode lets "
             "the bot read a group. The two values data/bot_telegram.json asks for.")
    info_parser.set_defaults(telegram_function=bot_info)

    use_bot_parser = subparsers.add_parser(
        "use-bot",
        help="Point THIS node at a bot, by secret ref - the mirror of `db use-store`. The id and "
             "username are read back from Telegram, never typed.")
    use_bot_parser.add_argument("--ref", required=True,
                               help="Secret ref holding the bot token, e.g. "
                                    "TOKEN_TELEGRAM_TEST_BOT.")
    use_bot_parser.add_argument("--dry-run", action="store_true",
                               help="Print what would be written, and write nothing.")

    level_parser = subparsers.add_parser(
        "group-level",
        help="Give a discovered group its notify level. save-updates finds groups but "
             "deliberately leaves them inert; this is the step that decides what each is for.")
    level_parser.add_argument("--group", required=True,
                              help="Group id, or its title (exact, or a substring naming one).")
    level_parser.add_argument("--level", required=True,
                              help="notify_level, e.g. logging|warning|error|critical|sla|"
                                   "backup|restore|sql|control|test. Empty string clears it.")
    level_parser.add_argument("--allow-command", type=int, default=None, dest="allow_command",
                              help="Minimum user level allowed to run commands here. Unset leaves "
                                   "it as it is; 0 means no commands from this group.")
    level_parser.set_defaults(telegram_function=set_group_level)

    user_level_parser = subparsers.add_parser(
        "user-level",
        help="Give a discovered user the level that decides which commands they may run. "
             "Intake records every sender at 0; this is the step that clears them.")
    user_level_parser.add_argument("--user", required=True,
                                   help="Numeric user id, or username (with or without @). "
                                        "No substring match: a level is a permission.")
    user_level_parser.add_argument("--level", required=True, type=int,
                                   help="user_type. A command with command_type N runs in a "
                                        "private chat for a user at N or above; 0 = public only.")
    user_level_parser.set_defaults(telegram_function=set_user_level)

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

        # No token is a *state*, not a fault. A fresh tool root has none until somebody creates a
        # bot, and `db-ops init` schedules this workflow every second — so treating "nothing to
        # deliver with" as an error wrote one to the log per second, on an install where nothing
        # was wrong. It also took `APP-CONTROL` down with it, which reports through the same chat.
        #
        # A token that is *named and missing* still raises: that is a real misconfiguration, and
        # `config.resolve_bot_token` is where it belongs.
        if args.command == "use-bot":
            # Before `_missing_bot_token`, deliberately: this is the command you reach for when
            # the token is missing or points at the wrong bot, and gating it on a working token
            # would make it unusable in exactly the situation it exists for.
            from db_ops.telegram.use_bot import UseBotError, use_bot

            settings = _telegram_settings_raw(config_path)
            try:
                result = use_bot(
                    args.ref,
                    data_dir=Path(config.telegram.bot_config_file).parent
                    if config.telegram.bot_config_file else Path(config_path).parent / "data",
                    api_url=config.telegram.api_url,
                    timeout_seconds=config.telegram.timeout_seconds,
                    dry_run=bool(args.dry_run),
                    telegram_settings=settings,
                    settings_path=getattr(config, "telegram_config_file", "") or "",
                )
            except UseBotError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        missing = _missing_bot_token(config)
        if missing:
            skipped = {
                "skipped": True,
                "reason": missing,
                "fix": ("create a bot with @BotFather, put the token in secrets/secret_text.json "
                        "under the ref named by telegram_bot_token_ref, then run "
                        "db-ops encrypt-secret"),
            }
            print(json.dumps(skipped, ensure_ascii=False, indent=2))
            return 0

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


def _telegram_settings_raw(config_path: Any) -> dict:
    """The telegram settings file as written, so `use-bot` can see a pin the parsed config hides.

    `parse_config` merges `telegram_bot_token_ref` from the settings file and the bot file into one
    value, which is exactly the thing `use-bot` has to tell apart: a ref set in the settings file
    WINS, so writing the bot file while one is pinned changes the name and not the token.
    """
    from db_ops.lib.json_io import load_json_file

    try:
        raw = load_json_file(Path(config_path))
        named = raw.get("telegram_config_file")
        if named:
            settings = load_json_file(Path(config_path).parent / str(named))
            return settings.get("telegram") if isinstance(settings.get("telegram"), dict) else settings
        return raw.get("telegram") if isinstance(raw.get("telegram"), dict) else {}
    except Exception:  # noqa: BLE001 - a settings file we cannot read pins nothing
        return {}


def _missing_bot_token(config: Any) -> str:
    """Why there is no usable bot token, or "" when there is one.

    Deliberately *not* the `enabled` flag: `enabled` decides routing, this decides whether the
    toolkit holds the one thing without which no Telegram command can do anything. It answers by
    resolving the token exactly as the sender would.

    **A named-but-absent ref counts as "not configured", not as a fault**, and that is the whole
    point. `db-ops init` writes `telegram_bot_token_ref` as a template — deliberately, because an
    earlier scaffold left it out and a real send failed with "bot token is empty", naming the
    symptom and not the missing field. So the shipped, correct, nothing-is-wrong state of a fresh
    tool root *is* a ref pointing at a secret nobody has added yet. Treating that as an error made
    a new install log one per second, and took `APP-CONTROL` down with it.

    The reason is returned rather than swallowed, so the caller can print what is missing. A
    mistyped ref is therefore still reported by name — it is just reported as "this is not set up"
    rather than as a crash, which is what it looks like from the outside either way.
    """
    try:
        token = str(config.telegram.resolved_bot_token or "").strip()
    except Exception as exc:  # noqa: BLE001 - a missing ref is a state; report it, do not raise.
        return str(exc)
    return "" if token else "no bot token is configured"


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
