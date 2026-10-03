"""The Telegram support commands: the replies a command sends - its result, an unknown command, the placeholders a reply text fills.

Split out of ``telegram/command_processor.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``command_processor`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
import json
import re
from typing import Any
from db_ops.lib.process_liveness import (  # noqa: F401 - re-exported, see above
    is_pid_alive as _is_pid_alive,
    is_windows_pid_alive as _is_windows_pid_alive,
    is_zombie as _is_zombie,
    process_start_marker,
    stop_process_tree,
)
from db_ops.lib.telegram_command_text import (  # noqa: F401 - re-exported, see above
    command_key_from_message,
    first_command_token,
    normalize_command_text,
    parse_command_message,
    split_with_verbatim_tail,
    render_command_line,
    split_command_tokens,
    strip_bot_username,
)
from db_ops.db.queue_message import queue_message, store_block_from
from db_ops.db import DbOpsStore
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.telegram.command_base import SupportCommand, TelegramCommandError


COMMAND_STATUS_DONE = 1
COMMAND_STATUS_SKIPPED = -1
COMMAND_STATUS_NOT_FOUND = -2
UNKNOWN_SUPPORT_COMMAND_HELP = "Please check the command name or use /spbot_status to verify the bot is running."


def queue_command_reply(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    message_text: str,
    source_id: str,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> int:
    reply_metadata = {
        "command_id": command.command_id,
        "command_text": command.command_text,
        "action_type": command.action_type,
        "status": status,
    }
    reply_metadata.update(metadata or {})
    return queue_message({
        "store": store_block_from(store),
        "chat_id": str(row["chat_id"]),
        "text": message_text,
        # A command reply reports the command's own outcome. Anything the vocabulary does not
        # recognise (a bespoke status string) resolves to no type, and the send layer falls
        # back to the header - never to a wrong symbol.
        "status": status,
        "reply_message_id": int(row["message_id"]) if row["message_id"] is not None else None,
        "note": f"Reply for command {command.command_text}",
        "source_type": "telegram_command_messages",
        "source_id": source_id,
        "metadata": reply_metadata,
    }, fallback_store=store)


def _close_interrupted_command(store: DbOpsStore, row: Any) -> None:
    """Close a command whose run was interrupted, and tell its sender - never run it again.

    A stale claim used to re-open the message, and fifteen minutes later the next pass ran the
    action a second time with nobody asking: `/spbot_kill_spid`, `/spbot_shrink_log`,
    `/spbot_start_job` and `/spbot_restart_server` included, while the first run's reply had
    perhaps never been sent, so the operator did not know it had run at all (review 0.25.0, B3.1).
    Whether it took effect is a question only the person who asked can answer.
    """
    text = str(row["text"] or "").strip()
    store.update_telegram_command_message_status(
        telegram_command_message_id=int(row["telegram_command_message_id"]),
        command_status=COMMAND_STATUS_SKIPPED,
        process_note="Interrupted before it finished; not run again (review 0.25.0, B3.1).",
    )
    queue_message({
        "store": store_block_from(store),
        "message_type": "plain",
        "chat_id": str(row["chat_id"]),
        "text": (f"{text}\nwas interrupted before it finished - the bot was restarted, or the command "
                 "ran past the bot's own time limit. It was NOT run again. Check whether it took "
                 "effect, and send it again if it is still needed."),
        "reply_message_id": int(row["message_id"]) if row["message_id"] is not None else None,
        "note": "Interrupted command, not retried",
        "source_type": "telegram_command_messages",
        "source_id": str(row["telegram_command_message_id"]),
        "metadata": {"status": "interrupted_not_retried"},
    }, fallback_store=store)


def queue_unknown_command_reply(
    *,
    store: DbOpsStore,
    row: Any,
    command_key: str,
) -> int:
    normalized_key = normalize_command_text(command_key)
    message_text = f"Unknown support command: /{normalized_key}\n\n{UNKNOWN_SUPPORT_COMMAND_HELP}"
    return queue_message({
        "store": store_block_from(store),
        "message_type": "plain",
        "chat_id": str(row["chat_id"]),
        "text": message_text,
        "reply_message_id": int(row["message_id"]) if row["message_id"] is not None else None,
        "note": f"Unknown support command: /{normalized_key}",
        "source_type": "telegram_command_messages",
        "source_id": str(row["telegram_command_message_id"]),
        "metadata": {
            "command_key": normalized_key,
            "status": "command_not_found",
        },
    }, fallback_store=store)


def telegram_username(row: Any) -> str:
    try:
        raw = json.loads(str(row["raw_json"] or "{}"))
    except (KeyError, TypeError, json.JSONDecodeError):
        return ""
    user = raw.get("from") or {}
    return str(user.get("username") or "")


def is_unknown_support_command(command_key: str) -> bool:
    return normalize_command_text(command_key).startswith("spbot_")


_PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_reply_text(
    template: str,
    *,
    row: Any,
    command: SupportCommand,
    args: list[str],
    action_result: dict[str, Any] | None,
    action_error: str | None,
) -> str:
    values = {
        "command_text": command.command_text,
        "arg_1": args[0] if len(args) >= 1 else "",
        "arg_2": args[1] if len(args) >= 2 else "",
        "arg_3": args[2] if len(args) >= 3 else "",
        "row_count": str((action_result or {}).get("row_count", "")),
        "status": "error" if action_error else "done",
        "error": action_error or "",
        "telegram_command_message_id": str(row["telegram_command_message_id"]),
    }
    # Additive: expose scalar action_result fields as {result_<key>} placeholders so
    # newer commands (e.g. add_sql_task) can echo ids/paths back. Existing templates
    # do not reference these, so their behaviour is unchanged.
    for key, value in (action_result or {}).items():
        if isinstance(value, (str, int, float, bool)):
            values[f"result_{key}"] = str(value)
    # Drop placeholders nothing filled BEFORE substituting, so a failed action replies
    # "rows= columns=" instead of the literal "rows={result_row_count}" (on error there is no
    # action_result at all). Done on the template only: a value that itself contains braces —
    # a SQL/driver error text inside {error} — must survive untouched.
    rendered = _PLACEHOLDER_PATTERN.sub(
        lambda match: match.group(0) if match.group(1) in values else "", template
    )
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def validate_target_ip(target_ip: str) -> str:
    import ipaddress

    value = str(target_ip or "").strip()
    if not value:
        raise TelegramCommandError("target_ip is required.", exit_code=2)
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise TelegramCommandError(f"Invalid target_ip: {value}.", exit_code=2) from exc
