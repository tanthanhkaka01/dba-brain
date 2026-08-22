from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from db_ops.common import data_sources
from db_ops.lib.listing import active_only, choice_lines, hidden_note
from db_ops.lib.secret_text import SECRET_KEY_ENV_VAR
from db_ops.config import DEFAULT_CONFIG_PATH, load_config
from db_ops.lib import common_cli
from db_ops.db.queue_message import queue_message, store_block_from
from db_ops.lib.time_window import MANUAL_ONLY
from db_ops.db.job_runs import telegram_log_metadata
from db_ops.db import DbOpsStore
from db_ops.logging_ops import log_event, setup_app_logger
from db_ops.telegram.commands import can_run_command
from db_ops.telegram.sql_commands import execute_sql_support_command
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module


DEFAULT_COMMANDS_PATH = DEFAULT_DATA_DIR / "telegram_support_commands.json"
# db_ops is a standalone repo root; keep REPO_ROOT as an alias so path resolution
# never escapes the project (was TOOL_ROOT.parents[1] under the old repo/tools/db_ops layout).
# A claim older than this is treated as abandoned (the owner was killed before it could mark
# the message done — e.g. the worker container was restarted mid-command), so the message is
# retried instead of being stuck pending forever.
CLAIM_STALE_SECONDS = 900
COMMAND_STATUS_DONE = 1
COMMAND_STATUS_SKIPPED = -1
COMMAND_STATUS_NOT_FOUND = -2
UNKNOWN_SUPPORT_COMMAND_HELP = "Please check the command name or use /spbot_status to verify the bot is running."

# Background CLI dispatch (e.g. /spbot_restore) writes a detailed trace here so a workflow
# that fails after "started" leaves an auditable record instead of silently stopping.
_DISPATCH_LOG_SCOPE = "telegram_dispatch"
_DISPATCH_LOGGERS: dict[str, Any] = {}


class TelegramCommandError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class SupportCommand:
    command_id: int
    command_text: str
    command_type: int
    reply_default: int
    reply_text: str
    is_group: int
    is_private: int
    need_file: int
    action_type: str = ""
    action_config: dict[str, Any] | None = None
    # Which cluster node handles this command: master | worker | all. Missing/empty
    # defaults to "worker" so legacy commands keep running on the worker as before.
    node_role: str = "worker"


def process_pending_command_messages(
    *,
    sqlite_path: str | Path,
    commands_path: str | Path = DEFAULT_COMMANDS_PATH,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    limit: int = 50,
) -> dict[str, int]:
    store = DbOpsStore(sqlite_path)
    rows = store.fetch_pending_telegram_command_messages(limit=limit)

    counts = {
        "read": len(rows),
        "processed": 0,
        "queued_reply": 0,
        "skipped": 0,
        "not_found": 0,
        "already_claimed": 0,
    }

    now = datetime.now(timezone.utc)
    claimed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_before = (now - timedelta(seconds=CLAIM_STALE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for row in rows:
        # A message is only marked done once its action finishes. The workflow runs every
        # second, so without an exclusive claim the next cycle re-reads the same pending row
        # and dispatches the command a second time (observed: five "started" replies for one
        # /spbot_report_hourly_metrics, and the collect running repeatedly).
        if not store.claim_telegram_command_message(
            telegram_command_message_id=int(row["telegram_command_message_id"]),
            claimed_at=claimed_at,
            stale_before=stale_before,
        ):
            counts["already_claimed"] += 1
            continue

        result = process_one_command_message(
            sqlite_path=sqlite_path,
            telegram_command_message_id=int(row["telegram_command_message_id"]),
            commands_path=commands_path,
            config_path=config_path,
        )
        counts["processed"] += int(result["processed"])
        counts["queued_reply"] += int(result["queued_reply"])
        counts["skipped"] += int(result["skipped"])
        counts["not_found"] += int(result["not_found"])

    return counts


def process_pending_conversation_messages(
    *,
    sqlite_path: str | Path,
    commands_path: str | Path = DEFAULT_COMMANDS_PATH,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    limit: int = 50,
) -> dict[str, int]:
    store = DbOpsStore(sqlite_path)
    commands = load_support_commands(commands_path)
    commands_by_id = {command.command_id: command for command in commands}
    states = store.fetch_waiting_telegram_conversation_states(limit=limit)
    counts = {
        "read": len(states),
        "waiting": 0,
        "processed": 0,
        "queued_reply": 0,
        "failed": 0,
        "already_claimed": 0,
    }

    now = datetime.now(timezone.utc)
    claimed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stale_before = (now - timedelta(seconds=CLAIM_STALE_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for state in states:
        message = store.fetch_next_telegram_message_for_state(
            chat_id=str(state["chat_id"]),
            user_id=str(state["user_id"]),
            after_message_id=int(state["wait_after_message_id"]),
        )
        if message is None:
            # Still waiting for the user. Claiming here would lock the state for the whole
            # stale window and the reply, when it comes, would sit unprocessed.
            counts["waiting"] += 1
            continue

        # Same exclusivity as pending command messages: a state stays 'waiting' while its
        # action runs, so an overlapping workflow cycle would act on the same user reply twice.
        if not store.claim_telegram_conversation_state(
            state_id=int(state["state_id"]), claimed_at=claimed_at, stale_before=stale_before,
        ):
            counts["already_claimed"] += 1
            continue
        command = commands_by_id.get(int(state["command_id"]))
        if command is None:
            store.update_telegram_conversation_state(
                state_id=int(state["state_id"]),
                status="error",
                state_data=state_json_dict(state),
                consumed_telegram_message_id=int(message["telegram_message_id"]),
                note=f"Command not found: {state['command_id']}",
            )
            counts["failed"] += 1
            continue

        state_data = state_json_dict(state)
        args = list(state_data.get("args") or [])
        parameter_position = int(state_data.get("parameter_position") or 1)
        while len(args) < parameter_position:
            args.append("")
        value = str(message["text"] or "").strip()

        # File-attachment support: if the awaited parameter accepts a file (e.g. the
        # add_sql_task SQL body) and the message carries a document instead of text,
        # download the file and use its contents as the value. Failures reply to the
        # user and mark the state as errored rather than crashing the processor.
        awaited = _parameter_at_position(command, parameter_position)
        if awaited is not None and awaited.get("accept_file") and not value:
            document = _message_document(message)
            if document is not None:
                try:
                    # A .sql body is text; a .xlsx is a zip, and decoding it as utf-8 either
                    # raises or silently mangles it. `file_encoding: "base64"` says the awaited
                    # parameter wants the bytes, carried the way a JSON request can carry them.
                    if str(awaited.get("file_encoding") or "").lower() == "base64":
                        value = _download_document_base64(document, config_path=config_path)
                    else:
                        value = _download_document_text(document, config_path=config_path)
                except Exception as exc:  # noqa: BLE001 - report and fail the state.
                    queue_message({
                        "store": store_block_from(store),
                        "message_type": "failed",
                        "chat_id": str(state["chat_id"]),
                        "text": f"Could not read the attached file: {safe_error_summary(exc)}",
                        "reply_message_id": int(message["message_id"]) if message["message_id"] is not None else None,
                        "note": f"File download failed for {command.command_text}",
                        "source_type": "telegram_conversation_states",
                        "source_id": str(state["state_id"]),
                        "metadata": {"command_id": command.command_id, "command_text": command.command_text},
                    }, fallback_store=store)
                    store.update_telegram_conversation_state(
                        state_id=int(state["state_id"]), status="error", state_data=state_data,
                        consumed_telegram_message_id=int(message["telegram_message_id"]),
                        note="file download failed")
                    counts["queued_reply"] += 1
                    counts["failed"] += 1
                    continue

        args[parameter_position - 1] = value

        next_missing = first_missing_prompt_parameter(command, args)
        if next_missing is not None:
            _chain_next_conversation_parameter(
                store=store,
                state=state,
                message=message,
                command=command,
                args=args,
                next_missing=next_missing,
                state_data=state_data,
            )
            counts["processed"] += 1
            counts["queued_reply"] += 1
            continue

        action_result: dict[str, Any] | None = None
        action_error: str | None = None
        try:
            action_result = execute_command_action(
                store=store,
                row=message,
                command=command,
                args=args,
                sqlite_path=sqlite_path,
                config_path=config_path,
                source_id=str(state["source_telegram_command_message_id"] or state["state_id"]),
            )
        except Exception as exc:  # noqa: BLE001 - reply to user and mark state failed.
            action_error = safe_error_summary(exc)

        reply_text = render_reply_text(
            command.reply_text,
            row={"telegram_command_message_id": state["source_telegram_command_message_id"] or ""},
            command=command,
            args=args,
            action_result=action_result,
            action_error=action_error,
        )
        if reply_text:
            queue_message({
                "store": store_block_from(store),
                "chat_id": str(state["chat_id"]),
                "text": reply_text,
                "reply_message_id": int(message["message_id"]) if message["message_id"] is not None else None,
                # action_error is the command's verdict: set means the action raised.
                "message_type": "failed" if action_error else "success",
                "note": f"Reply for conversation command {command.command_text}",
                "source_type": "telegram_conversation_states",
                "source_id": str(state["state_id"]),
                "metadata": {
                    "command_id": command.command_id,
                    "command_text": command.command_text,
                    "action_type": command.action_type,
                    "action_result": action_result,
                    "action_error": action_error,
                },
            }, fallback_store=store)
            counts["queued_reply"] += 1

        state_data["args"] = args
        state_data["action_result"] = action_result
        state_data["action_error"] = action_error
        store.update_telegram_conversation_state(
            state_id=int(state["state_id"]),
            status="error" if action_error else "done",
            state_data=state_data,
            consumed_telegram_message_id=int(message["telegram_message_id"]),
            note=action_error or "processed",
        )
        if action_error:
            counts["failed"] += 1
        else:
            counts["processed"] += 1

    return counts


def find_support_command_by_key(
    command_key: str,
    commands: list[SupportCommand],
) -> SupportCommand | None:
    normalized_key = normalize_command_text(command_key)

    matched: SupportCommand | None = None
    matched_len = -1

    for command in commands:
        command_text = normalize_command_text(command.command_text)

        if normalized_key == command_text or normalized_key.startswith(f"{command_text}_"):
            if len(command_text) > matched_len:
                matched = command
                matched_len = len(command_text)

    return matched

def process_one_command_message(
    *,
    sqlite_path: str | Path,
    telegram_command_message_id: int,
    commands_path: str | Path = DEFAULT_COMMANDS_PATH,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
) -> dict[str, int | str]:
    store = DbOpsStore(sqlite_path)
    row = store.fetch_telegram_command_message(telegram_command_message_id=telegram_command_message_id)
    if row is None:
        return {
            "telegram_command_message_id": telegram_command_message_id,
            "processed": 0,
            "queued_reply": 0,
            "skipped": 0,
            "not_found": 1,
            "status": "row_not_found",
        }
    if int(row["command_status"]) != 0:
        return {
            "telegram_command_message_id": telegram_command_message_id,
            "processed": 0,
            "queued_reply": 0,
            "skipped": 1,
            "not_found": 0,
            "status": "not_pending",
        }

    commands = load_support_commands(commands_path)
    parsed_message = parse_command_message(str(row["text"] or ""))
    command_key = parsed_message["command_key"] or command_key_from_message(
        str(row["command_prefix"] or ""),
        str(row["command_payload"] or ""),
    )
    command = find_support_command_by_key(command_key, commands)

    if command is None:
        queued_reply = 0
        queued_reply_id = None
        if is_unknown_support_command(command_key):
            queued_reply_id = queue_unknown_command_reply(
                store=store,
                row=row,
                command_key=command_key,
            )
            queued_reply = 1
        store.update_telegram_command_message_status(
            telegram_command_message_id=telegram_command_message_id,
            command_status=COMMAND_STATUS_NOT_FOUND,
            process_note=(
                f"Command not found: {command_key}"
                + (f"; queued_unknown_command_reply={queued_reply_id}" if queued_reply_id is not None else "")
            ),
        )
        return {
            "telegram_command_message_id": telegram_command_message_id,
            "processed": 0,
            "queued_reply": queued_reply,
            "skipped": 0,
            "not_found": 1,
            "status": "command_not_found",
        }

    node_role = _resolve_node_role(config_path)
    if not _command_runs_on_node(command.node_role, node_role):
        store.update_telegram_command_message_status(
            telegram_command_message_id=telegram_command_message_id,
            command_status=COMMAND_STATUS_SKIPPED,
            process_note=(
                f"Command {command.command_text} runs on node_role={command.node_role}; "
                f"this node is {node_role}. Left for the other node."
            ),
        )
        return {
            "telegram_command_message_id": telegram_command_message_id,
            "processed": 0,
            "queued_reply": 0,
            "skipped": 1,
            "not_found": 0,
            "status": "skipped_wrong_node_role",
        }

    # Only a negative command_type disables a command. command_type=0 is the public tier
    # (runs for everyone) and must fall through to the permission check, not be treated as off.
    if command.command_type < 0:
        store.update_telegram_command_message_status(
            telegram_command_message_id=telegram_command_message_id,
            command_status=COMMAND_STATUS_SKIPPED,
            process_note=f"Command disabled: {command.command_text}",
        )
        return {
            "telegram_command_message_id": telegram_command_message_id,
            "processed": 0,
            "queued_reply": 0,
            "skipped": 1,
            "not_found": 0,
            "status": "command_disabled",
        }

    permission = command_permission(
        row=row,
        command=command,
        data_dir=Path(commands_path).resolve().parent,
    )
    if not permission["allowed"]:
        queued_reply_id = queue_permission_denied_reply(
            store=store,
            row=row,
            command=command,
            permission=permission,
        )
        store.update_telegram_command_message_status(
            telegram_command_message_id=telegram_command_message_id,
            command_status=COMMAND_STATUS_SKIPPED,
            command_id=command.command_id,
            process_note=f"{permission['reason']}; queued_permission_denied_reply={queued_reply_id}",
        )
        return {
            "telegram_command_message_id": telegram_command_message_id,
            "processed": 0,
            "queued_reply": 1,
            "skipped": 1,
            "not_found": 0,
            "status": "permission_denied",
        }

    queued_reply_id = None
    queued_reply = 0
    action_result: dict[str, Any] | None = None
    action_error: str | None = None
    if command.action_type in {"sql_execute", "cli_execute", "add_sql_task", "sql_to_xlsx", "list_server_id", "list_all_command", "list_sql_tasks", "list_metrics", "metric_toggle", "create_table_from_xlsx"}:
        missing_parameter = first_missing_prompt_parameter(command, parsed_message["args"])
        if missing_parameter is not None:
            return queue_missing_parameter_prompt(
                store=store,
                row=row,
                command=command,
                missing_parameter=missing_parameter,
                args=parsed_message["args"],
            )
        try:
            action_result = execute_command_action(
                store=store,
                row=row,
                command=command,
                args=parsed_message["args"],
                sqlite_path=sqlite_path,
                config_path=config_path,
                source_id=str(row["telegram_command_message_id"]),
            )
        except Exception as exc:  # noqa: BLE001 - command failure should be reported back to Telegram.
            action_error = safe_error_summary(exc)
        if action_result is not None and int(action_result.get("_queued_reply_count", 0) or 0) > 0:
            queued_reply += int(action_result.get("_queued_reply_count", 0) or 0)

    if command.reply_default == 1 and command.reply_text:
        reply_text = render_reply_text(
            command.reply_text,
            row=row,
            command=command,
            args=parsed_message["args"],
            action_result=action_result,
            action_error=action_error,
        )
        queued_reply_id = queue_message({
            "store": store_block_from(store),
            "chat_id": str(row["chat_id"]),
            "text": reply_text,
            "reply_message_id": int(row["message_id"]) if row["message_id"] is not None else None,
            "message_type": "failed" if action_error else "success",
            "note": f"Reply for command {command.command_text}",
            "source_type": "telegram_command_messages",
            "source_id": str(row["telegram_command_message_id"]),
            "metadata": {
                "command_id": command.command_id,
                "command_text": command.command_text,
                "action_type": command.action_type,
                "action_result": action_result,
                "action_error": action_error,
            },
        }, fallback_store=store)
        queued_reply = 1

    process_note = f"Command matched: {command.command_text}"
    if action_result is not None:
        process_note = f"{process_note}; action={command.action_type}; row_count={action_result.get('row_count')}"
    if action_error is not None:
        process_note = f"{process_note}; action_error={action_error}"
    if queued_reply_id is not None:
        process_note = f"{process_note}; queued_reply={queued_reply_id}"

    store.update_telegram_command_message_status(
        telegram_command_message_id=telegram_command_message_id,
        command_status=COMMAND_STATUS_SKIPPED if action_error else COMMAND_STATUS_DONE,
        command_id=command.command_id,
        process_note=process_note,
    )
    return {
        "telegram_command_message_id": telegram_command_message_id,
        "processed": 0 if action_error else 1,
        "queued_reply": queued_reply,
        "skipped": 1 if action_error else 0,
        "not_found": 0,
        "status": "action_failed" if action_error else "processed",
    }


def load_support_commands(path: str | Path = DEFAULT_COMMANDS_PATH) -> list[SupportCommand]:
    with Path(path).open("r", encoding="utf-8-sig") as file:
        data = json.load(file)

    commands: list[SupportCommand] = []
    for item in data.get("telegram_support_commands", []):
        commands.append(
            SupportCommand(
                command_id=int(item["command_id"]),
                command_text=str(item["command_text"]),
                command_type=int(item.get("command_type", 0)),
                reply_default=int(item.get("reply_default", 0)),
                reply_text=str(item.get("reply_text") or ""),
                is_group=int(item.get("is_group", 0)),
                is_private=int(item.get("is_private", 0)),
                need_file=int(item.get("need_file", 0)),
                action_type=str(item.get("action_type") or ""),
                action_config=dict(item.get("action_config") or {}),
                node_role=(str(item.get("node_role") or "worker").strip().lower() or "worker"),
            )
        )
    return commands


def _command_runs_on_node(command_node_role: str, node_role: str) -> bool:
    """Telegram support-command role match, mirroring the app-command daemon. ``all``/``both``/
    ``any`` run on every node; otherwise the command's role must equal the node's role. A
    command with no role defaults to ``worker`` (set at load), so undefined/legacy ``spbot_*``
    commands keep being handled by the worker as before."""
    role = (command_node_role or "worker").strip().lower()
    if role in ("all", "both", "any"):
        return True
    return role == (node_role or "master").strip().lower()


def _resolve_node_role(config_path: str | Path | None = None) -> str:
    """This node's role for telegram command routing. The Telegram workflow is a worker-side
    app (APP-TELEGRAM runs with DB_OPS_NODE_ROLE=worker), so the role comes from that env and
    **defaults to ``worker``** when unset — that keeps legacy/undefined commands on the worker
    and matches where the processor actually runs. Set DB_OPS_NODE_ROLE=master to route the
    ``master``/``all`` commands on a master-side processor."""
    role = (os.getenv("DB_OPS_NODE_ROLE", "") or "").strip().lower()
    return role if role in ("master", "worker") else "worker"


def command_permission(*, row: Any, command: SupportCommand, data_dir: Path) -> dict[str, Any]:
    chat_type = str(row["chat_type"] or "")
    chat_id = str(row["chat_id"] or "")
    user_id = str(row["user_id"] or "")
    # Chat type is decided by the command's own is_private/is_group flags, not by the user's
    # level or the group's allow_command — so the reason must say so, or the operator goes off
    # raising a permission that was never the blocker.
    if chat_type == "private" and command.is_private != 1:
        return {"allowed": False,
                "reason": "This command is not available in a private chat (is_private=0); run it in a group."}
    if chat_type != "private" and command.is_group != 1:
        return {"allowed": False,
                "reason": "This command is not available in a group (is_group=0); message the bot in a private chat."}

    user_type = telegram_user_type(data_dir / "telegram_users.json", user_id=user_id)
    allow_command = user_type if chat_type == "private" else telegram_group_allow_command(data_dir / "telegram_groups.json", chat_id=chat_id)
    allowed = can_run_command(
        allow_command=allow_command,
        user_type=user_type,
        command_type=command.command_type,
    )
    if allowed:
        return {"allowed": True, "reason": "allowed"}
    chat_label = "private chat" if chat_type == "private" else "this group"
    return {
        "allowed": False,
        "reason": (
            f"The user or chat permission is not enough to run this command in {chat_label} "
            f"(command_type={command.command_type}, user_type={user_type}, allow_command={allow_command})."
        ),
    }


def telegram_group_allow_command(path: Path, *, chat_id: str) -> int:
    # Through the one reader (common.data_sources); this app still owns what the records mean.
    for item in data_sources.load_telegram_groups(path):
        if str(item.get("group_id", "")) == chat_id and str(item.get("status", "active")) == "active":
            return int(item.get("allow_command", 0))
    return 0


def telegram_user_type(path: Path, *, user_id: str) -> int:
    for item in data_sources.load_telegram_users(path):
        if str(item.get("user_id", "")) == user_id and str(item.get("status", "active")) == "active":
            return int(item.get("user_type", 0))
    return 0


def queue_permission_denied_reply(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    permission: dict[str, Any],
) -> int:
    chat_type = str(row["chat_type"] or "")
    message_text = permission_denied_reply_text(
        chat_type=chat_type, command=command, reason=str(permission.get("reason") or ""),
    )
    return queue_message({
        "store": store_block_from(store),
        "message_type": "failed",
        "chat_id": str(row["chat_id"]),
        "text": message_text,
        "reply_message_id": int(row["message_id"]) if row["message_id"] is not None else None,
        "note": f"Permission denied for command {command.command_text}",
        "source_type": "telegram_command_messages",
        "source_id": str(row["telegram_command_message_id"]),
        "metadata": {
            "command_id": command.command_id,
            "command_text": command.command_text,
            "status": "permission_denied",
            "reason": str(permission.get("reason", "")),
        },
    }, fallback_store=store)


def permission_denied_reply_text(*, chat_type: str, command: SupportCommand, reason: str = "") -> str:
    """Say *which* rule refused, not just "permission denied".

    A command restricted to private chat and a command the user's level cannot run are two
    different problems with two different fixes; a single generic sentence sent the operator to
    check group_type when the command was simply not allowed in a group at all."""
    chat_label = "private chat" if chat_type == "private" else "this group"
    detail = reason.strip() or (
        f"The user or chat permission is not enough to run this command in {chat_label}."
    )
    return f"Permission denied for /{command.command_text}. {detail} Please contact the bot admin."


def load_json_object(path: Path, root_key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    return list(data.get(root_key, []))


def state_json_dict(state: Any) -> dict[str, Any]:
    try:
        return dict(json.loads(str(state["state_json"] or "{}")))
    except json.JSONDecodeError:
        return {}


def first_missing_prompt_parameter(command: SupportCommand, args: list[str]) -> dict[str, Any] | None:
    config = dict(command.action_config or {})
    parameters = list(config.get("parameters") or [])
    for parameter in parameters:
        position = int(parameter.get("position", 1))
        required = bool(parameter.get("required", True)) or prompt_condition_holds(
            parameter, parameters, args
        )
        value = args[position - 1] if len(args) >= position else ""
        if required and str(value).strip() == "" and parameter.get("prompt_text"):
            skipped_value = skip_parameter_value(parameter, parameters, args)
            if skipped_value is None:
                return dict(parameter)
            while len(args) < position:
                args.append("")
            args[position - 1] = skipped_value
    return None


def prompt_condition_holds(
    parameter: dict[str, Any], parameters: list[dict[str, Any]], args: list[str]
) -> bool:
    """Whether an *optional* parameter must be asked for on this particular run.

    The mirror of ``skip_when``: that one declines to ask a question that cannot apply, this one
    asks a question that only some runs need. ``/spbot_run_sql_task`` is why it exists — its
    ``task_params`` is optional because most tasks declare no parameters, so it was never
    prompted, and a task that *requires* one ran with none and failed with a message about a
    missing ``--param`` that the operator was never given a chance to supply::

        "prompt_when": {"condition": "sql_task_has_parameters", "parameter": "sql_id"}

    ``sql_task_has_parameters`` holds when the sql_id already answered names a task that declares
    parameters in ``sql_commands.json``. A config read failure never blocks the flow: the run
    proceeds exactly as it did before, which is the behaviour every task without parameters wants.
    """
    rule = parameter.get("prompt_when")
    if not isinstance(rule, dict):
        return False
    if str(rule.get("condition") or "") != "sql_task_has_parameters":
        return False
    source_name = str(rule.get("parameter") or "sql_id").strip()
    source = next((item for item in parameters if str(item.get("name") or "") == source_name), None)
    if source is None:
        return False
    position = int(source.get("position", 1))
    sql_id = str(args[position - 1] if len(args) >= position else "").strip()
    return bool(sql_task_parameter_names(sql_id))


def sql_tasks_listing(sql_id: str | int | None = None) -> dict[str, Any]:
    """Ask the **sql_tasks app** what tasks exist, through its own CLI.

    This app does not read ``sql_commands.json``. It used to, and then it disagreed with the app
    that runs those tasks: whether a task counted as runnable was decided twice, and a task's
    declared parameters were not part of the bot's picture at all — so ``/spbot_run_sql_task``
    never asked for one and every run of a task that required a parameter failed. The owner
    answers instead (``python -m db_ops.sql_tasks.cli list-tasks``), which is also the boundary
    the rest of db_ops keeps: apps talk through ``common`` or through each other's CLI, never by
    parsing each other's config.

    Returns the CLI's JSON object, or ``{"ok": False, "error": ...}``. Never raises: a listing
    that cannot be produced is reported to the operator, and a prompt decision that cannot be
    made falls back to not prompting — the behaviour that was correct for every task before
    parameters existed.
    """
    argv = [sys.executable, "-m", "db_ops.sql_tasks.cli", "list-tasks"]
    if str(sql_id or "").strip():
        argv += ["--sql-id", str(sql_id).strip()]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, cwd=str(TOOL_ROOT),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _dispatch_log(
            None, f"telegram.command_processor.sql_tasks_cli.failed|error={safe_error_summary(exc)}",
            level="warning")
        return {"ok": False, "error": safe_error_summary(exc)}
    if completed.returncode != 0 or not (completed.stdout or "").strip():
        error = (completed.stderr or completed.stdout or "no output").strip()[:500]
        _dispatch_log(
            None,
            f"telegram.command_processor.sql_tasks_cli.failed|exit={completed.returncode}|"
            f"error={error}",
            level="warning")
        return {"ok": False, "error": error}
    try:
        return dict(json.loads(completed.stdout))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        _dispatch_log(
            None,
            f"telegram.command_processor.sql_tasks_cli.badjson|error={safe_error_summary(exc)}",
            level="warning")
        return {"ok": False, "error": f"sql_tasks list-tasks did not answer with JSON: {exc}"}


def sql_task_parameter_names(sql_id: str) -> list[str]:
    """The parameter names a SQL task declares, or ``[]`` when it declares none."""
    if not str(sql_id).strip().isdigit():
        return []
    listing = sql_tasks_listing(sql_id)
    if not listing.get("ok"):
        return []
    for task in listing.get("sql_tasks") or []:
        if str(task.get("sql_id")) == str(sql_id).strip():
            return [str(name) for name in (task.get("parameter_names") or [])]
    return []


def render_prompt_text(parameter: dict[str, Any], parameters: list[dict[str, Any]],
                       args: list[str]) -> str:
    """The prompt as the operator sees it, with ``{sql_task_parameters}`` filled in.

    A prompt that says "values for the parameters this task declares" and then leaves the person
    to guess the names is only half an answer — they have just chosen a task by number, not by
    reading its config. Naming them turns the prompt into something answerable.
    """
    text = str(parameter.get("prompt_text") or f"Please input {parameter.get('name', 'value')}:")
    if "{sql_task_parameters}" not in text:
        choices = prompt_choice_text(parameter, parameters, args)
        return f"{text}\n\n{choices}" if choices else text
    rule = parameter.get("prompt_when") if isinstance(parameter.get("prompt_when"), dict) else {}
    source_name = str((rule or {}).get("parameter") or "sql_id").strip()
    source = next((item for item in parameters if str(item.get("name") or "") == source_name), None)
    sql_id = ""
    if source is not None:
        position = int(source.get("position", 1))
        sql_id = str(args[position - 1] if len(args) >= position else "").strip()
    names = sql_task_parameter_names(sql_id)
    return text.replace("{sql_task_parameters}", ", ".join(names) if names else "none")


#: A prompt that lists choices runs a `common` CLI command *while the operator waits*, so it needs
#: a deadline of its own. `list-databases` opens a connection to the server, and the default of no
#: timeout would leave the conversation with no prompt at all when an instance is unreachable —
#: the one situation in which the plain prompt is most needed.
PROMPT_CHOICES_TIMEOUT_SECONDS = 25


def prompt_choice_text(parameter: dict[str, Any], parameters: list[dict[str, Any]],
                       args: list[str]) -> str:
    """The list of values this parameter will accept, or ``""`` when it cannot be produced.

    Declared per parameter in ``telegram_support_commands.json``::

        "prompt_choices": {"command": "list-schemas", "data_key": "schemas",
                           "request": {"target": "{server_id}", "database": "{database}"}}

    ``{name}`` in the request is filled from the answer already given for that parameter, which is
    what makes the two steps of ``/spbot_xlsx_to_table`` chain: the database list needs the server
    just answered, and the schema list needs both.

    **Every failure returns an empty string.** A prompt is the only thing that keeps the flow
    moving, and the cases where listing fails — unreachable instance, wrong credential, an engine
    `list-schemas` does not know — are exactly the cases where the operator still needs to be asked
    the question. Typing the name has always worked and still does; the list is an aid, never a
    gate. This is the same fail-open contract as ``sql_task_parameter_names`` and
    ``_target_has_no_database``.
    """
    rule = parameter.get("prompt_choices")
    if not isinstance(rule, dict):
        return ""
    command = str(rule.get("command") or "").strip()
    data_key = str(rule.get("data_key") or "").strip()
    template = rule.get("request")
    if not command or not data_key or not isinstance(template, dict):
        return ""

    answered = _answered_parameters(parameters, args)
    request: dict[str, Any] = {}
    for key, value in template.items():
        resolved = _fill_placeholders(value, answered)
        if isinstance(resolved, str) and not resolved.strip():
            # A placeholder with no answer yet means this list cannot be asked for. Happens when a
            # command is invoked with its arguments out of order; the bare prompt is the answer.
            return ""
        request[str(key)] = resolved

    try:
        success, data, _error = common_cli.run_allowing_failure(
            command, request, timeout_seconds=PROMPT_CHOICES_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - listing is an aid; nothing here may block the prompt.
        return ""
    if not success or not isinstance(data, dict):
        return ""

    entries = data.get(data_key)
    if not isinstance(entries, list):
        return ""
    names = [
        str(entry.get("name") or "") if isinstance(entry, dict) else str(entry)
        for entry in entries
    ]
    return choice_lines(names)


def _answered_parameters(parameters: list[dict[str, Any]], args: list[str]) -> dict[str, str]:
    """``parameter name -> the answer given so far``, for the ones that have one."""
    answered: dict[str, str] = {}
    for item in parameters:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        position = int(item.get("position", 1))
        answered[name] = str(args[position - 1] if len(args) >= position else "").strip()
    return answered


def _fill_placeholders(value: Any, answered: dict[str, str]) -> Any:
    """Substitute ``{parameter_name}`` in a request value; non-strings pass through untouched."""
    if not isinstance(value, str):
        return value
    filled = value
    for name, answer in answered.items():
        filled = filled.replace("{" + name + "}", answer)
    return filled


def skip_parameter_value(
    parameter: dict[str, Any], parameters: list[dict[str, Any]], args: list[str]
) -> str | None:
    """Value to auto-fill instead of prompting the user, or None to prompt as usual.

    A parameter may declare ``skip_when`` in the command config, e.g.::

        "skip_when": {"condition": "target_has_no_database", "parameter": "target_ip", "value": "-"}

    ``target_has_no_database`` holds when every db instance configured for that IP has no
    db_type — an OS-only host such as an ERP AOS application VM. Asking such a host for
    db_type or port is meaningless, so the bot fills the skip value and runs the command.
    """
    rule = parameter.get("skip_when")
    if not isinstance(rule, dict) or str(rule.get("condition") or "") != "target_has_no_database":
        return None
    source_name = str(rule.get("parameter") or "").strip()
    source = next((item for item in parameters if str(item.get("name") or "") == source_name), None)
    if source is None:
        return None
    source_position = int(source.get("position", 1))
    target_ip = str(args[source_position - 1] if len(args) >= source_position else "").strip()
    if not target_ip or not _target_has_no_database(target_ip):
        return None
    return str(rule.get("value") or "-")


def _target_has_no_database(target_ip: str) -> bool:
    from db_ops.common.data_sources import load_config_metric_targets

    try:
        targets = [item for item in load_config_metric_targets() if str(item.ip) == target_ip]
    except Exception:  # noqa: BLE001 - a config read failure must not block the prompt flow.
        return False
    return bool(targets) and all(not str(item.db_type or "").strip() for item in targets)


def queue_missing_parameter_prompt(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    missing_parameter: dict[str, Any],
    args: list[str],
) -> dict[str, int | str]:
    prompt_text = render_prompt_text(
        missing_parameter, list((command.action_config or {}).get("parameters") or []), args,
    )
    queued_reply_id = queue_message({
        "store": store_block_from(store),
        "message_type": "plain",
        "chat_id": str(row["chat_id"]),
        "text": prompt_text,
        "reply_message_id": int(row["message_id"]) if row["message_id"] is not None else None,
        "note": f"Prompt for command {command.command_text}",
        "source_type": "telegram_command_messages",
        "source_id": str(row["telegram_command_message_id"]),
        "metadata": {
            "command_id": command.command_id,
            "command_text": command.command_text,
            "conversation_state": "waiting",
            "force_reply": True,
        },
    }, fallback_store=store)
    state_id = store.upsert_telegram_conversation_state(
        chat_id=str(row["chat_id"]),
        user_id=str(row["user_id"]),
        command_id=command.command_id,
        command_text=command.command_text,
        state_key=str(missing_parameter.get("name") or "arg"),
        wait_after_message_id=int(row["message_id"]),
        source_telegram_command_message_id=int(row["telegram_command_message_id"]),
        state_data={
            "args": args,
            "parameter_name": missing_parameter.get("name"),
            "parameter_position": int(missing_parameter.get("position", 1)),
        },
    )
    process_note = f"Command matched: {command.command_text}; waiting_state={state_id}; queued_prompt={queued_reply_id}"
    store.update_telegram_command_message_status(
        telegram_command_message_id=int(row["telegram_command_message_id"]),
        command_status=COMMAND_STATUS_DONE,
        command_id=command.command_id,
        process_note=process_note,
    )
    return {
        "telegram_command_message_id": int(row["telegram_command_message_id"]),
        "processed": 1,
        "queued_reply": 1,
        "skipped": 0,
        "not_found": 0,
        "status": "waiting_for_input",
    }


def _chain_next_conversation_parameter(
    *,
    store: DbOpsStore,
    state: Any,
    message: Any,
    command: SupportCommand,
    args: list[str],
    next_missing: dict[str, Any],
    state_data: dict[str, Any],
) -> None:
    # Mark current state done BEFORE upsert — upsert sets all 'waiting' to 'replaced' first.
    updated_state_data = dict(state_data)
    updated_state_data["args"] = args
    store.update_telegram_conversation_state(
        state_id=int(state["state_id"]),
        status="done",
        state_data=updated_state_data,
        consumed_telegram_message_id=int(message["telegram_message_id"]),
        note="chained to next parameter",
    )
    prompt_text = render_prompt_text(
        next_missing, list((command.action_config or {}).get("parameters") or []), args,
    )
    queue_message({
        "store": store_block_from(store),
        "message_type": "plain",
        "chat_id": str(state["chat_id"]),
        "text": prompt_text,
        "reply_message_id": int(message["message_id"]) if message["message_id"] is not None else None,
        "note": f"Prompt for command {command.command_text}",
        "source_type": "telegram_conversation_states",
        "source_id": str(state["state_id"]),
        "metadata": {
            "command_id": command.command_id,
            "command_text": command.command_text,
            "conversation_state": "waiting",
            "force_reply": True,
        },
    }, fallback_store=store)
    store.upsert_telegram_conversation_state(
        chat_id=str(state["chat_id"]),
        user_id=str(state["user_id"]),
        command_id=command.command_id,
        command_text=command.command_text,
        state_key=str(next_missing.get("name") or "arg"),
        wait_after_message_id=int(message["message_id"]),
        source_telegram_command_message_id=int(state["source_telegram_command_message_id"] or state["state_id"]),
        state_data={
            "args": args,
            "parameter_name": next_missing.get("name"),
            "parameter_position": int(next_missing.get("position", 1)),
        },
    )


def execute_command_action(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    args: list[str],
    sqlite_path: str | Path,
    config_path: str | Path,
    source_id: str,
) -> dict[str, Any]:
    if command.action_type == "sql_execute":
        return execute_sql_support_command(command=command, args=args)
    if command.action_type == "cli_execute":
        return execute_configured_cli_command(
            store=store,
            row=row,
            command=command,
            args=args,
            config_path=config_path,
            source_id=source_id,
        )
    if command.action_type == "add_sql_task":
        return execute_add_sql_task_command(command=command, args=args)
    if command.action_type == "sql_to_xlsx":
        return execute_sql_to_xlsx_command(
            store=store, row=row, command=command, args=args, source_id=source_id
        )
    if command.action_type == "list_server_id":
        return execute_list_server_id_command()
    if command.action_type == "list_all_command":
        return execute_list_all_command_command(
            store=store, row=row, command=command, source_id=source_id
        )
    if command.action_type == "list_sql_tasks":
        return execute_list_sql_tasks_command(store=store, row=row, command=command, source_id=source_id)
    if command.action_type == "list_metrics":
        return execute_list_metrics_command(store=store, row=row, command=command, source_id=source_id)
    if command.action_type == "metric_toggle":
        return execute_metric_toggle_command(command=command, args=args)
    if command.action_type == "create_table_from_xlsx":
        return execute_create_table_from_xlsx_command(command=command, args=args)
    return {}


def execute_create_table_from_xlsx_command(*, command: SupportCommand,
                                           args: list[str]) -> dict[str, Any]:
    """Create a table from an attached file: /spbot_xlsx_to_table.

    Four answers, collected by the ordinary prompt loop — server_id, database, schema, then the
    file itself. The file arrives as base64 because the awaited parameter declares
    ``file_encoding: "base64"``; from there it is the same JSON object
    :mod:`db_ops.common.table_load` takes from a shell, so the Telegram path and the CLI path
    cannot drift. An .xlsx and a delimited text file are both accepted and both look identical
    here — `table_load` decides which it is from the bytes.

    ``table_name`` is deliberately **not** prompted for. The common case is "I need this
    queryable now", the generated ``temp_<random>`` answers it, and one more prompt between an
    operator and the thing they wanted is a step at which people give up. The name is in the
    reply, which is what they need to find it again. Someone who wants to choose passes it as a
    fifth word on the command line.
    """
    def _arg(position: int) -> str:
        return str(args[position - 1]).strip() if len(args) >= position else ""

    request: dict[str, Any] = {
        "target": _arg(1),
        "database": _arg(2),
        "schema": _arg(3),
        "file_base64": _arg(4),
        "table_name": _arg(5),
    }
    # A command may pin any of these in its own config; the config is the one place that decides
    # whether this deployment lets a Telegram user drop an existing table.
    for key in ("if_exists", "load_rows", "text_length", "max_rows", "credential_name",
                "delimiter"):
        if key in (command.action_config or {}):
            request[key] = (command.action_config or {})[key]

    if not request["file_base64"]:
        raise TelegramCommandError(
            "No file received. Attach the .xlsx or delimited text file to the message that "
            "answers the last prompt.", exit_code=2)
    # Through the `common` CLI: building a table from a spreadsheet and loading it is work on a
    # customer database, and the request above is already the exact JSON that command takes — the
    # Telegram path and a shell caller hand over the same object.
    try:
        data = common_cli.run("create-table-from-xlsx", request)
    except common_cli.CommonCliError as exc:
        raise TelegramCommandError(str(exc), exit_code=1) from exc

    # Keys are returned **unprefixed**. `render_reply_text` exposes each scalar as
    # `{result_<key>}` itself, so returning `result_server_id` here becomes
    # `{result_result_server_id}` and the template renders it as nothing — which is exactly what
    # shipped on 2026-08-13: a reply that said "done" over blank fields, so the operator could
    # not tell which table had been created or whether anything had.
    return {
        "status": "success",
        "server_id": data["server_id"],
        "database": data["database"],
        "schema": data["schema"],
        "table_name": data["table_name"],
        "qualified_name": data["qualified_name"],
        "column_count": data["column_count"],
        "column_type": data["column_type"],
        "rows_inserted": data["rows_inserted"],
        # How the file was read. On a text file the delimiter is a guess, and a wrong guess makes
        # a table whose columns look plausible — this line is where the operator sees it.
        "source_format": _describe_source(data),
        # `row_count` is one of the renderer's own top-level placeholders, not just a
        # `result_*` one, so the generic `{row_count}` in any template still fills.
        "row_count": data["rows_inserted"],
    }


def _describe_source(data: dict[str, Any]) -> str:
    """``xlsx (first sheet)`` / ``tab-delimited text (utf-16-le)`` — one line for the reply."""
    if str(data.get("source_format") or "") != "delimited":
        return "xlsx (first sheet)"
    from db_ops.lib import delimited_import

    return (f"{delimited_import.describe_delimiter(str(data.get('source_delimiter') or ''))} "
            f"text ({data.get('source_encoding') or 'utf-8'})")


def execute_list_server_id_command() -> dict[str, Any]:
    """Build the server-target listing for /spbot_list_server_id (reply via {result_listing})."""
    from db_ops.common import data_sources as target_resolve

    targets = target_resolve.list_target_instances()
    return {
        "listing": target_resolve.format_target_list(),
        "target_count": len(targets),
    }


# A Telegram message caps at 4096 chars; listings longer than this are sent as a JSON document.
TELEGRAM_LISTING_TEXT_LIMIT = 3500


def _command_parameter_summary(config: dict[str, Any]) -> str:
    """``<target> <format> <sql_text...>`` — the arguments, in the order they are typed.

    Derived from the command's own ``parameters`` block, so a command that gains an argument
    describes itself correctly the moment its config changes. Optional arguments are bracketed,
    and a ``consume_rest`` one gets an ellipsis: "the rest of the message goes here" is the single
    thing operators most often get wrong, and it is the reason `/spbot_sql_export` had to put its
    format argument before the SQL rather than after it.
    """
    parameters = sorted(
        (item for item in (config.get("parameters") or []) if isinstance(item, dict)),
        key=lambda item: int(item.get("position") or 0),
    )
    parts: list[str] = []
    for parameter in parameters:
        name = str(parameter.get("name") or "").strip() or "arg"
        if parameter.get("consume_rest"):
            name += "..."
        parts.append(f"<{name}>" if parameter.get("required") else f"[{name}]")
    return " ".join(parts)


def execute_list_all_command_command(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    source_id: str,
) -> dict[str, Any]:
    """Every command this bot answers, built from ``telegram_support_commands.json`` itself.

    Nothing in the reply is written by hand. A listing that must be edited when a command is added
    is a listing that is wrong the first time somebody forgets — which has already happened twice
    to the Markdown doc (``tests/test_listing.py`` says so in its own docstring), and that doc at
    least has a test guarding it. So this reads the same config the dispatcher reads and describes
    each command from its own entry: arguments from its ``parameters``, clearance from its
    ``command_type``, where it may be typed from ``is_private`` / ``is_group``.

    **Only what the caller can actually run is listed.** Offering a command the permission check
    will refuse is the failure :mod:`db_ops.lib.listing` exists to prevent — it invites someone
    to type something that cannot work — so commands above the caller's clearance, and commands
    that do not run in this kind of chat, are dropped and counted rather than silently omitted.
    """
    from db_ops.lib import listing as listing_lib

    data_dir = TOOL_ROOT / "data"
    entries = [
        entry for entry in load_json_object(data_dir / "telegram_support_commands.json",
                                            "telegram_support_commands")
        if isinstance(entry, dict)
    ]

    keys = row.keys() if hasattr(row, "keys") else {}
    chat_type = str((row["chat_type"] if "chat_type" in keys else "") or "private")
    user_id = str((row["user_id"] if "user_id" in keys else "") or "")
    user_type = telegram_user_type(data_dir / "telegram_users.json", user_id=user_id)
    allow_command = (
        user_type if chat_type == "private"
        else telegram_group_allow_command(data_dir / "telegram_groups.json",
                                          chat_id=str(row["chat_id"]))
    )

    # A negative command_type is how a command is switched off (see process_one_command_message),
    # so that — not an `active` key — is what decides "runnable" here.
    runnable, disabled = listing_lib.active_only(
        entries, key=lambda entry: int(entry.get("command_type", 0) or 0) >= 0,
    )
    cleared, above_clearance = listing_lib.active_only(
        runnable,
        key=lambda entry: can_run_command(
            allow_command=allow_command,
            user_type=user_type,
            command_type=int(entry.get("command_type", 0) or 0),
        ),
    )
    here, wrong_chat = listing_lib.active_only(
        cleared,
        key=lambda entry: bool(
            entry.get("is_private") == 1 if chat_type == "private" else entry.get("is_group") == 1
        ),
    )
    here = sorted(here, key=lambda entry: str(entry.get("command_text") or ""))

    lines = [f"Bot commands you can run here: {len(here)}"]
    for entry in here:
        arguments = _command_parameter_summary(dict(entry.get("action_config") or {}))
        clearance = int(entry.get("command_type", 0) or 0)
        suffix = "" if clearance == 0 else f"  [clearance {clearance}]"
        lines.append(f"/{entry.get('command_text')} {arguments}".rstrip() + suffix)

    # Each reason is counted separately: "12 hidden" tells an operator nothing, while "above your
    # clearance" and "only runs in a group" are two different things to do about it.
    notes = []
    if above_clearance:
        notes.append(f"({above_clearance} hidden: above your clearance.)")
    if wrong_chat:
        where = "a group" if chat_type == "private" else "a private chat"
        notes.append(f"({wrong_chat} hidden: they only run in {where}.)")
    if disabled:
        notes.append(f"({disabled} hidden: turned off with command_type < 0.)")
    if notes:
        lines.extend(["", *notes])
    listing = "\n".join(lines)

    result: dict[str, Any] = {
        "command_count": len(here),
        "hidden_count": above_clearance + wrong_chat + disabled,
    }
    if len(listing) > TELEGRAM_LISTING_TEXT_LIMIT:
        file_path = _queue_listing_document(
            store=store, row=row, command=command, source_id=source_id,
            payload={"commands": [
                {"command_text": entry.get("command_text"),
                 "arguments": _command_parameter_summary(dict(entry.get("action_config") or {})),
                 "command_type": entry.get("command_type"),
                 "action_type": entry.get("action_type")}
                for entry in here
            ]},
            file_prefix="bot_commands",
            caption=f"Bot commands you can run here: {len(here)}. Full list attached as JSON.",
        )
        result["listing"] = (
            lines[0] + "\nThe listing is too long for one message - full JSON file attached."
        )
        result["file_path"] = file_path
        result["_queued_reply_count"] = 1
    else:
        result["listing"] = listing
    return result


def _format_time_window_line(window: dict[str, Any] | None) -> str:
    """Compact one-line time-window text: only the set bounds + repeat/timeout."""
    window = window if isinstance(window, dict) else {}
    # A manual entry keeps its day/hour bounds in the JSON, but nothing ever consults them.
    # Printing "day 1..31 hour 0..23" would tell the operator it runs all day, every day.
    if window.get("repeat_interval") == MANUAL_ONLY:
        timeout = window.get("timeout")
        suffix = f" timeout {timeout}s" if timeout is not None else ""
        return f"manual (run with /spbot_run_sql_task){suffix}"
    parts: list[str] = []
    for name in ("year", "month", "day", "hour", "minute"):
        from_value = window.get(f"from_{name}")
        to_value = window.get(f"to_{name}")
        if from_value is None and to_value is None:
            continue
        parts.append(f"{name} {'-' if from_value is None else from_value}..{'-' if to_value is None else to_value}")
    repeat = window.get("repeat_interval")
    if repeat == 0:
        parts.append("run-once")
    elif repeat is not None:
        parts.append(f"every {repeat}s")
    timeout = window.get("timeout")
    if timeout is not None:
        parts.append(f"timeout {timeout}s")
    return " ".join(parts) or "always"


def _queue_listing_document(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    source_id: str,
    payload: dict[str, Any],
    file_prefix: str,
    caption: str,
) -> str:
    """Write ``payload`` as a JSON file and queue it back as a Telegram document."""
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    output_dir = resolve_result_output_dir("runtime/output/telegram/config_exports").resolve()
    if not is_relative_to(output_dir, TOOL_ROOT):
        raise TelegramCommandError(
            f"Refusing to create result folder outside tools/db_ops: {output_dir}", exit_code=2
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    file_name = safe_output_file_name(f"{file_prefix}_{timestamp}.json")
    file_path = (output_dir / file_name).resolve()
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    queue_message({
        "store": store_block_from(store),
        "message_type": "plain",
        "chat_id": str(row["chat_id"]),
        "text": caption,
        "reply_message_id": int(row["message_id"]) if row["message_id"] is not None else None,
        "note": f"Document for command {command.command_text}",
        "source_type": "telegram_command_messages",
        "source_id": source_id,
        "metadata": {
            "command_id": command.command_id,
            "command_text": command.command_text,
            "action_type": command.action_type,
            "status": "success_document",
            "document_path": str(file_path),
        },
    }, fallback_store=store)
    return str(file_path)


def execute_list_sql_tasks_command(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    source_id: str,
) -> dict[str, Any]:
    """List every configured SQL task with its targets and time windows
    (/spbot_list_sql_tasks, reply via {result_listing}). A listing longer than one
    Telegram message is sent as a JSON document instead.

    The tasks come from the sql_tasks app's own CLI (:func:`sql_tasks_listing`) — including
    which of them count as runnable, which this app used to decide for itself and could
    therefore get wrong. All that is left here is turning them into lines someone can read on a
    phone, which is this app's job and nobody else's.
    """
    listing_data = sql_tasks_listing()
    if not listing_data.get("ok"):
        return {
            "command_count": 0,
            "target_count": 0,
            "listing": (
                "Could not read the SQL task list from the sql_tasks app: "
                f"{listing_data.get('error') or 'unknown error'}"
            ),
        }

    sql_tasks = list(listing_data.get("sql_tasks") or [])
    command_count = int(listing_data.get("command_count") or 0)
    target_count = int(listing_data.get("target_count") or 0)

    lines = [f"SQL tasks: {command_count} command(s), {target_count} target(s)"]
    for item in sql_tasks:
        lines.append(f"#{item.get('sql_id')} {item.get('sql_code') or ''}")
        # What the operator has to be ready to supply before choosing this number in
        # /spbot_run_sql_task. Required ones are marked: that is the difference between a task
        # that runs on its own and one that will ask a question back.
        parameters = list(item.get("parameters") or [])
        if parameters:
            required = set(item.get("required_parameter_names") or [])
            rendered = ", ".join(
                f"{name}{'*' if name in required else ''}"
                for name in (item.get("parameter_names") or [])
            )
            lines.append(f"  params: {rendered}   (* = required)")
        for target in item.get("targets") or []:
            database_name = str(target.get("database_name") or "-")
            window_text = _format_time_window_line(target.get("time_window"))
            output_format = str(target.get("output_format") or "").strip()
            output_text = f" output={output_format}" if output_format else ""
            method = str(target.get("sql_access_method") or "direct")
            via_text = "" if method == "direct" else f" via={method}"
            lines.append(
                f"  -> {target.get('server_id') or '?'} db={database_name} "
                f"{window_text}{output_text}{via_text}"
            )
    note = hidden_note(int(listing_data.get("hidden_count") or 0), noun="entry")
    if note:
        lines.extend(["", note])
    listing = "\n".join(lines)

    result: dict[str, Any] = {
        "command_count": command_count,
        "target_count": target_count,
    }
    if len(listing) > TELEGRAM_LISTING_TEXT_LIMIT:
        file_path = _queue_listing_document(
            store=store, row=row, command=command, source_id=source_id,
            payload={"sql_tasks": sql_tasks},
            file_prefix="sql_tasks",
            caption=(
                f"SQL task list: {command_count} command(s), {target_count} target(s). "
                "Full configuration attached as JSON."
            ),
        )
        result["listing"] = lines[0] + "\nThe listing is too long for one message — full JSON file attached."
        result["file_path"] = file_path
        result["_queued_reply_count"] = 1
    else:
        result["listing"] = listing
    return result


def execute_list_metrics_command(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    source_id: str,
) -> dict[str, Any]:
    """List every metric definition with its repeat interval (/spbot_list_metrics,
    reply via {result_listing}); sends a JSON document when too long for one message."""
    from db_ops.lib.time_window import parse_time_window_config

    # metric_definitions.json belongs to metrics and is read in exactly one place
    # (common.data_sources) since 2026-08-15; this app only lists what is in it.
    metrics = data_sources.load_metric_definition_records(data_dir=TOOL_ROOT / "data")

    entries: list[dict[str, Any]] = []
    for item in sorted(metrics, key=lambda entry: str(entry.get("metric_code") or "")):
        metric_code = str(item.get("metric_code") or "").strip()
        if not metric_code:
            continue
        try:
            window = parse_time_window_config(item, context=f"metric_definitions.{metric_code}").time_window
        except RuntimeError:
            continue
        entries.append({
            "metric_code": metric_code,
            "db_type": str(item.get("db_type") or ""),
            "collector_type": str(item.get("collector_type") or ""),
            "active": bool(item.get("active", True)),
            "repeat_interval": window.repeat_interval,
            "timeout": window.timeout,
        })
    entries, hidden = active_only(entries)

    lines = [f"Metrics: {len(entries)} definition(s)"]
    for entry in entries:
        interval = entry["repeat_interval"]
        interval_text = "run-once" if interval == 0 else (f"every {interval}s" if interval is not None else "every -")
        lines.append(f"{entry['metric_code']} [{entry['collector_type']}/{entry['db_type']}] {interval_text}")
    note = hidden_note(hidden, noun="metric")
    if note:
        lines.extend(["", note])
    listing = "\n".join(lines)

    result: dict[str, Any] = {"metric_count": len(entries)}
    if len(listing) > TELEGRAM_LISTING_TEXT_LIMIT:
        file_path = _queue_listing_document(
            store=store, row=row, command=command, source_id=source_id,
            payload={"metrics": entries},
            file_prefix="metrics",
            caption=f"Metric list: {len(entries)} definition(s) with time windows. Attached as JSON.",
        )
        result["listing"] = lines[0] + "\nThe listing is too long for one message — full JSON file attached."
        result["file_path"] = file_path
        result["_queued_reply_count"] = 1
    else:
        result["listing"] = listing
    return result


def execute_metric_toggle_command(*, command: SupportCommand, args: list[str]) -> dict[str, Any]:
    """Enable/disable metric collection for one server (/spbot_metric_toggle).

    args: ``server_id`` ``on|off`` ``scope`` where scope is ``all``,
    ``collector:<sql|cmd|docker|k8s>``, or one metric_code. The config write goes through
    ``python -m db_ops.common.cli metric-toggle`` — the same atomic ``db_instances.json`` update
    an operator gets at a shell, reached the same way, so the bot cannot drift from the CLI."""
    from db_ops.lib import common_cli

    server_id = str(args[0]).strip() if len(args) >= 1 else ""
    state = str(args[1]).strip().lower() if len(args) >= 2 else ""
    scope = str(args[2]).strip() if len(args) >= 3 else ""
    if state not in {"on", "off"}:
        raise TelegramCommandError("state must be 'on' or 'off'.", exit_code=2)
    try:
        result = common_cli.run("metric-toggle",
                                {"server_id": server_id, "state": state, "scope": scope})
    except common_cli.CommonCliError as exc:
        raise TelegramCommandError(str(exc), exit_code=1) from exc
    detail_parts = list(result.get("changes") or []) + list(result.get("warnings") or [])
    return {
        "status": "changed" if result.get("changed") else "no change",
        "server_id": result.get("server_id"),
        "scope": result.get("scope"),
        "state": state,
        "detail": "\n".join(detail_parts) or "-",
    }


def _format_argument_position(config: dict[str, Any]) -> int | None:
    """The 1-based position of a ``format`` parameter, or ``None`` when the command has none.

    Read from the command's own ``parameters`` list rather than hard-coded, so ``action_config``
    stays the single place that says which argument is which. A command that does not declare
    ``format`` keeps taking it from ``action_config`` — which is how ``/spbot_sql_to_xlsx``
    goes on producing exactly the file its name promises, with its argument order untouched.
    """
    for parameter in config.get("parameters") or []:
        if str((parameter or {}).get("name") or "").strip().lower() == "format":
            position = int((parameter or {}).get("position") or 0)
            return position or None
    return None


def execute_sql_to_xlsx_command(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    args: list[str],
    source_id: str,
) -> dict[str, Any]:
    """Run a read-only SELECT on the target (arg 1) using ``sql_text`` (arg 2), write the first
    result set to an .xlsx, and queue it back as a Telegram document. The target is a server_id
    or a ``<db_type> <ip> [port]`` spec (see :func:`db_ops.common.sql_run.resolve_sqlserver_target`).

    The SELECT-only contract is enforced in :mod:`db_ops.common.sql_run` (the connection
    is never committed and any affected rows are rejected + rolled back). A known failure raises
    :class:`TelegramCommandError` so the reply template echoes it to the user; the document is
    queued only on success.
    """
    from db_ops.telegram import sql_commands
    from db_ops.lib import result_format
    from db_ops.lib.task_output import FILE_OUTPUT_FORMATS
    from db_ops.lib.xlsx_export import MAX_CELL_TEXT

    config = dict(command.action_config or {})
    # Which file the operator gets. A command may fix it in action_config (that is what keeps
    # /spbot_sql_to_xlsx producing exactly what its name promises) or declare a `format`
    # PARAMETER, in which case it is an argument the caller supplies. Read from the declared
    # parameters rather than a hard-coded index, so the config stays the one place that says
    # which argument is which.
    format_position = _format_argument_position(config)
    if format_position:
        index = format_position - 1
        export_format = str(args[index]).strip() if len(args) > index else ""
        args = list(args[:index]) + list(args[index + 1:])
    else:
        export_format = str(config.get("format") or "xlsx")
    try:
        export_format = result_format.normalize_format(export_format)
    except result_format.ResultFormatError as exc:
        raise TelegramCommandError(str(exc), exit_code=2) from exc
    if export_format not in FILE_OUTPUT_FORMATS:
        # `raw` and `json` render fine but are not what someone asking for a document wants:
        # raw exists to be piped in a shell, and neither opens in anything an operator has.
        raise TelegramCommandError(
            f"format must be one of {', '.join(FILE_OUTPUT_FORMATS)}; got {export_format!r}.",
            exit_code=2,
        )

    # arg 1 is the target: a server_id, or a "<db_type> <ip> [port]" spec delivered as one
    # message via the conversation prompt. Inline, only the single-token server_id form works
    # (a multi-word spec would collide with the consume_rest SQL text).
    target = str(args[0]).strip() if len(args) >= 1 else ""
    # sql_text is a consume_rest parameter: an inline command splits the SQL across tokens, while
    # the conversation flow delivers the whole pasted message as a single arg. Joining args[1:]
    # reconstructs both.
    sql_text = " ".join(str(part) for part in args[1:]).strip()
    if not target:
        raise TelegramCommandError("target (server_id, or '<db_type> <ip> [port]') is required.", exit_code=2)
    if not sql_text:
        raise TelegramCommandError("sql_text is required.", exit_code=2)

    max_rows = int(config.get("max_rows") or sql_commands.DEFAULT_SQL_TO_XLSX_MAX_ROWS)
    timeout_seconds = int(config.get("connect_timeout_seconds") or sql_commands.DEFAULT_CONNECT_TIMEOUT_SECONDS)
    try:
        result = sql_commands.run_sql_to_xlsx(
            target=target,
            sql_text=sql_text,
            # Optional: pin the database the SQL runs in, so a query does not have to open with
            # "USE <db>;". Unset = the target instance's own database.
            database=str(config.get("database") or config.get("database_name") or ""),
            # Optional: run as a named login from users.json instead of the instance default
            # (which is often a DBA account). Set it to a read-only credential where one exists.
            credential_name=str(config.get("credential_name") or ""),
            max_rows=max_rows,
            timeout_seconds=timeout_seconds,
        )
    except sql_commands.SqlToXlsxError as exc:
        # A known, user-facing failure (unknown server_id / non-SELECT / connect / SQL error):
        # report it verbatim so the reply template can show it.
        raise TelegramCommandError(safe_error_summary(exc), exit_code=1) from exc

    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    output_dir = resolve_result_output_dir(
        str(config.get("output_dir") or "runtime/output/telegram/sql_to_xlsx")
    ).resolve()
    if not is_relative_to(output_dir, TOOL_ROOT):
        raise TelegramCommandError(
            f"Refusing to create result folder outside tools/db_ops: {output_dir}", exit_code=2
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    file_name = safe_output_file_name(
        render_template(
            str(config.get("file_name_template") or "sql_to_xlsx_{server_id}_{timestamp}"),
            {"server_id": result["server_id"], "timestamp": timestamp},
        )
    )
    if not file_name.lower().endswith(f".{export_format}"):
        file_name += f".{export_format}"
    file_path = (output_dir / file_name).resolve()
    if not is_relative_to(file_path, TOOL_ROOT):
        raise TelegramCommandError(
            f"Refusing to write result file outside tools/db_ops: {file_path}", exit_code=2
        )

    # One call whatever the format: db_ops.lib.result_format owns which formats write
    # themselves and which return text, so this handler and the sql_tasks exporter cannot drift
    # into two files that look different for the same query.
    written = result_format.write_result(
        {"ok": True, "columns": result["columns"], "rows": result["rows"],
         "row_count": result["row_count"]},
        fmt=export_format,
        path=file_path,
        sheet_name="Result",
    )

    notes = []
    if result["truncated"]:
        notes.append(" (truncated to the row limit)")
    if written.get("truncated_cells"):
        # Excel drops a string over 32,767 chars and calls the file damaged, so the writer cuts
        # it. Say so: a silently shortened query_plan / query_sql_text is worse than a warning.
        notes.append(
            f" ({written['truncated_cells']} cell(s) cut to Excel's {MAX_CELL_TEXT:,}-character limit)"
        )
    caption = render_template(
        str(
            config.get("caption")
            or "SQL result for {server_id} ({database}): {row_count} row(s){truncated_note}."
        ),
        {
            "server_id": result["server_id"],
            "database": result["database"],
            "row_count": result["row_count"],
            "truncated_note": "".join(notes),
        },
    )
    queue_message({
        "store": store_block_from(store),
        "message_type": "plain",
        "chat_id": str(row["chat_id"]),
        "text": caption,
        "reply_message_id": int(row["message_id"]) if row["message_id"] is not None else None,
        "note": f"Document for command {command.command_text}",
        "source_type": "telegram_command_messages",
        "source_id": source_id,
        "metadata": {
            "command_id": command.command_id,
            "command_text": command.command_text,
            "action_type": command.action_type,
            "status": "success_document",
            "document_path": str(file_path),
        },
    }, fallback_store=store)
    return {
        "server_id": result["server_id"],
        "database": result["database"],
        # Available to reply templates as {result_username} / {result_credential_name}.
        "credential_name": result.get("credential_name", ""),
        "username": result.get("username", ""),
        "row_count": result["row_count"],
        "affected_rows": result["affected_rows"],
        "truncated": result["truncated"],
        "column_count": len(result["columns"]),
        # 0 for every format but xlsx: only Excel has a per-cell length limit to hit.
        "truncated_cells": written.get("truncated_cells", 0),
        "file_path": str(file_path),
        "file_name": file_path.name,
        "_queued_reply_count": 1,
    }


def _parameter_at_position(command: SupportCommand, position: int) -> dict[str, Any] | None:
    for parameter in (command.action_config or {}).get("parameters") or []:
        try:
            if int(parameter.get("position", 0)) == int(position):
                return dict(parameter)
        except (TypeError, ValueError):
            continue
    return None


def _message_document(message: Any) -> dict[str, Any] | None:
    """Return the inbound Telegram document dict (file_id, ...) from the message row, if any."""
    try:
        raw = message["raw_json"]
    except (KeyError, IndexError, TypeError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    document = data.get("document") if isinstance(data, dict) else None
    if isinstance(document, dict) and document.get("file_id"):
        return document
    return None


def _download_document_text(document: dict[str, Any], *, config_path: str | Path) -> str:
    """Download an attached document and decode it as text (utf-8, BOM tolerant)."""
    from db_ops.config import load_config
    from db_ops.telegram import api

    config = load_config(config_path)
    data = api.get_file_bytes(
        bot_token=config.telegram.resolved_bot_token,
        file_id=str(document["file_id"]),
        api_url=config.telegram.api_url,
    )
    text = data.decode("utf-8-sig").strip()
    if not text:
        raise RuntimeError("Attached file is empty.")
    return text


def _download_document_base64(document: dict[str, Any], *, config_path: str | Path) -> str:
    """Download an attached document and return it base64-encoded.

    For a binary attachment — a workbook, an archive — where decoding as text would either raise
    or, worse, succeed against a zip's bytes and hand the action something that is no longer the
    file. base64 is what a JSON request can carry, so the value goes straight into the
    `common` CLI payload with nothing else to agree on.
    """
    import base64

    from db_ops.config import load_config
    from db_ops.telegram import api

    config = load_config(config_path)
    data = api.get_file_bytes(
        bot_token=config.telegram.resolved_bot_token,
        file_id=str(document["file_id"]),
        api_url=config.telegram.api_url,
    )
    if not data:
        raise RuntimeError("Attached file is empty.")
    return base64.b64encode(data).decode("ascii")


_ADD_SQL_NONE_TOKENS = {"", "-", "none", "null", "skip", "na", "n/a"}

# The schedule answer that means "never run this on a timer"; it becomes
# time_window.repeat_interval = MANUAL_ONLY (-1), the convention every scheduler shares.
MANUAL_SCHEDULE_WORD = "manual"


def _add_sql_optional(value: str) -> str | None:
    text = str(value or "").strip()
    return None if text.lower() in _ADD_SQL_NONE_TOKENS else text


def parse_add_sql_time_window(spec: str) -> dict[str, int] | None:
    """Parse a Telegram time-window step into a config_admin time_window dict.

    Accepts ``default``/empty (→ engine defaults), ``manual`` (→ ``repeat_interval = -1``, the
    shared MANUAL_ONLY convention: never scheduled, forced runs only), or up to four
    space/comma separated integers ``from_hour to_hour repeat_interval timeout``.
    """
    text = str(spec or "").strip()
    if text.lower() == MANUAL_SCHEDULE_WORD:
        return {"repeat_interval": MANUAL_ONLY}
    if text.lower() in {"", "default", "-", "none"}:
        return None
    parts = [p for p in re.split(r"[\s,]+", text) if p]
    keys = ("from_hour", "to_hour", "repeat_interval", "timeout")
    window: dict[str, int] = {}
    for key, part in zip(keys, parts):
        try:
            window[key] = int(part)
        except ValueError as exc:
            raise ValueError(f"time_window value for {key} must be an integer, got {part!r}") from exc
    return window or None


def execute_add_sql_task_command(*, command: SupportCommand, args: list[str]) -> dict[str, Any]:
    """Register + enable a new SQL task from the collected conversation parameters.

    Parameter order (from action_config.parameters): server_id, sql_name, schedule, output,
    sql_text. **db_type, instance and target database are not asked for** — they are already
    recorded against the server in ``db_instances.json``, so asking made the conversation four
    messages longer and let the operator enter values that do not resolve (that is how
    SQLSERVER-017 got a null instance and a task that could never find its database).

    All file and config mutation goes through ``python -m db_ops.common.cli add-sql`` — the same
    JSON object, the same atomic writes and the same validation an operator gets at a shell.
    Two things are still decided here, and both are *reads*, not writes: the schedule word is
    Telegram's own vocabulary (``manual`` / ``default`` / four integers), and the target's
    db_type, instance and credential come from ``data_sources``, the one reader of the data
    folder — the operator was never asked for them, so they have to be looked up before the
    request can be built.

    Always returns a result dict (never raises) so the reply template can echo the outcome;
    ``result_status`` is ``OK`` or ``FAILED``.
    """
    from db_ops.common import data_sources
    from db_ops.lib import common_cli, task_output

    def arg(position: int) -> str:
        return str(args[position - 1]).strip() if len(args) >= position else ""

    empty_result = {"status": "FAILED", "sql_id": "", "sql_code": "", "script_path": "",
                    "server_id": arg(1), "schedule": "", "output": ""}
    try:
        window = parse_add_sql_time_window(arg(3))
        output = task_output.normalize_output(arg(4))
        resolved = data_sources.resolve_sql_target_fields(arg(1))
        result = common_cli.run("add-sql", {
            "db_type": resolved["db_type"],
            "server_id": resolved["server_id"],
            "service_name": resolved["service_name"],
            "instance_name": resolved["instance_name"],
            "credential_name": resolved["credential_name"],
            "sql_name": arg(2),
            "sql_text": arg(5),
            "output": output,
            # The window's four keys are the command's own flags, so they travel as fields rather
            # than as a nested object — `add-sql` has exactly one parser and this is what it reads.
            **(window or {}),
        })
    except (common_cli.CommonCliError, data_sources.TargetResolveError, ValueError) as exc:
        return {**empty_result, "error": str(exc), "_queued_reply_count": 0}
    return {
        "status": "OK",
        "sql_id": result["sql_id"],
        "sql_code": result["sql_code"],
        "script_path": result["script_path"],
        "server_id": result["server_id"],
        "schedule": MANUAL_SCHEDULE_WORD if result["manual_only"] else "scheduled",
        "output": result["output"],
        "error": "",
        "_queued_reply_count": 0,
    }


def execute_configured_cli_command(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    args: list[str],
    config_path: str | Path,
    source_id: str,
) -> dict[str, Any]:
    try:
        values = cli_action_values(
            command=command, args=args, config_path=config_path,
            chat_id=str(row["chat_id"]) if row["chat_id"] is not None else None,
        )
    except Exception as exc:
        error_summary = safe_error_summary(exc)
        queue_command_reply(
            store=store,
            row=row,
            command=command,
            message_text=error_summary,
            source_id=source_id,
            status="validation_error",
        )
        raise
    config = dict(command.action_config or {})
    if bool(config.get("requires_secret_key")) and not os.environ.get(SECRET_KEY_ENV_VAR, "").strip():
        message = (
            f"Required secret key is not available ({SECRET_KEY_ENV_VAR}). "
            "Start the worker with --key/--key-base64 or set the secret key env."
        )
        queue_command_reply(
            store=store,
            row=row,
            command=command,
            message_text=message,
            source_id=source_id,
            status="validation_error",
        )
        raise TelegramCommandError(message, exit_code=2)
    if bool(config.get("background") or config.get("detached")):
        return execute_cli_background_command(
            store=store,
            row=row,
            command=command,
            values=values,
            source_id=source_id,
        )
    target_ip = str(values.get("target_ip") or "")
    start_time = datetime.now(timezone.utc)
    start_text = str((command.action_config or {}).get("start_text") or "Command started.")
    queue_command_reply(
        store=store,
        row=row,
        command=command,
        message_text=render_template(start_text, values),
        source_id=source_id,
        status="started",
    )
    file_result_config = dict((command.action_config or {}).get("result_file") or {})
    file_result: dict[str, Any] | None = None
    try:
        result = run_configured_cli_command(command=command, values=values)
        success_values = values | result
        if file_result_config:
            file_result = create_sql_run_result_file(
                store=store,
                config=file_result_config,
                values=success_values,
            )
            result.update(file_result)
    except Exception as exc:
        exit_code = int(getattr(exc, "exit_code", 1))
        # This path still holds the raw values, so the run's own secrets are scrubbed
        # literally — not just the shapes a pattern anticipates.
        error_summary = safe_error_summary(exc, secrets=secret_values(values))
        end_time = datetime.now(timezone.utc)
        failure_values = values | {"exit_code": exit_code, "error_summary": error_summary}
        failure_text = str(
            (command.action_config or {}).get("failure_text")
            or "Command failed.\nExit code: {exit_code}\nError: {error_summary}"
        )
        queue_command_reply(
            store=store,
            row=row,
            command=command,
            message_text=render_template(failure_text, failure_values),
            source_id=source_id,
            status="failed",
            metadata=telegram_log_metadata(
                telegram_user_id=str(row["user_id"] or ""),
                telegram_username=telegram_username(row),
                telegram_command=command.command_text,
                target_ip=target_ip,
                target_id=str(values.get("target_id") or ""),
                start_time=start_time,
                end_time=end_time,
                status="failed",
                error_summary=error_summary,
            )
            | {"cli_result": getattr(exc, "result", {})},
        )
        raise TelegramCommandError(error_summary, exit_code=exit_code) from exc

    end_time = datetime.now(timezone.utc)
    success_values = values | result
    success_text = str((command.action_config or {}).get("success_text") or "Command completed.")
    success_reply_id = queue_command_reply(
        store=store,
        row=row,
        command=command,
        message_text=render_template(success_text, success_values),
        source_id=source_id,
        status="success",
        metadata=telegram_log_metadata(
            telegram_user_id=str(row["user_id"] or ""),
            telegram_username=telegram_username(row),
            telegram_command=command.command_text,
            target_ip=target_ip,
            target_id=str(result.get("target_id") or ""),
            start_time=start_time,
            end_time=end_time,
            status="success",
            error_summary="",
        )
        | {"cli_result": sanitized_cli_result(result)},
    )
    queued_reply_count = 2
    if file_result_config and file_result is not None:
        document_caption = render_template(
            str(file_result_config.get("caption") or "Generated file: {file_name}"),
            success_values | file_result,
        )
        queue_message({
            "store": store_block_from(store),
            "message_type": "plain",
            "chat_id": str(row["chat_id"]),
            "text": document_caption,
            "reply_message_id": int(row["message_id"]) if row["message_id"] is not None else None,
            "note": f"Document for command {command.command_text}",
            "source_type": "telegram_command_messages",
            "source_id": source_id,
            "metadata": {
                "command_id": command.command_id,
                "command_text": command.command_text,
                "action_type": command.action_type,
                "status": "success_document",
                "document_path": file_result["file_path"],
                "success_reply_id": success_reply_id,
                "file_result": file_result,
            },
        }, fallback_store=store)
        queued_reply_count += 1
    result["_queued_reply_count"] = queued_reply_count
    return result


def create_sql_run_result_file(
    *,
    store: DbOpsStore,
    config: dict[str, Any],
    values: dict[str, Any],
) -> dict[str, Any]:
    source = str(config.get("source") or "latest_sql_run_result")
    if source != "latest_sql_run_result":
        raise TelegramCommandError(f"Unsupported result_file source: {source}", exit_code=2)
    sql_id = int(config.get("sql_id") or values.get("sql_id") or 0)
    if sql_id <= 0:
        raise TelegramCommandError("result_file.sql_id is required.", exit_code=2)
    sql_run = store.fetch_latest_sql_run_for_sql_id(sql_id=sql_id, status=str(config.get("status") or "done"))
    if sql_run is None:
        raise TelegramCommandError(f"No completed SQL run found for sql_id={sql_id}.", exit_code=1)

    result = json.loads(str(sql_run["result_json"] or "{}"))
    result_text = extract_result_column_text(result, column_name=str(config.get("result_column") or "ResultJson"))
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    output_root = resolve_result_output_dir(str(config.get("output_dir") or "runtime/output/telegram"))
    folder_template = str(config.get("folder_name_template") or "")
    folder_name = ""
    if folder_template:
        folder_name = safe_output_path_component(
            render_template(
                folder_template,
                values
                | {
                    "sql_id": sql_id,
                    "sql_run_id": int(sql_run["sql_run_id"]),
                    "timestamp": timestamp,
                },
            )
        )
    output_dir = (output_root / folder_name).resolve() if folder_name else output_root.resolve()
    if not is_relative_to(output_dir, TOOL_ROOT):
        raise TelegramCommandError(f"Refusing to create result folder outside tools/db_ops: {output_dir}", exit_code=2)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TelegramCommandError(f"Cannot create result folder: {output_dir}: {exc}", exit_code=1) from exc
    file_name = render_template(
        str(config.get("file_name_template") or "sql_task_{sql_id}_{timestamp}.json"),
        values
        | {
            "sql_id": sql_id,
            "sql_run_id": int(sql_run["sql_run_id"]),
            "timestamp": timestamp,
        },
    )
    file_name = safe_output_file_name(file_name)
    if not file_name.lower().endswith(".json"):
        file_name += ".json"
    file_path = (output_dir / file_name).resolve()
    if not is_relative_to(file_path, TOOL_ROOT):
        raise TelegramCommandError(f"Refusing to write result file outside tools/db_ops: {file_path}", exit_code=2)
    file_path.write_text(result_text, encoding="utf-8")
    if bool(config.get("validate_json", True)):
        try:
            json.loads(result_text)
        except json.JSONDecodeError as exc:
            raise TelegramCommandError(f"Result JSON validation failed; raw file kept at {file_path}: {exc}", exit_code=1) from exc
    return {
        "file_path": str(file_path),
        "file_name": file_path.name,
        "folder_name": output_dir.name,
        "folder_path": str(output_dir),
        "file_size": file_path.stat().st_size,
        "sql_id": sql_id,
        "sql_run_id": int(sql_run["sql_run_id"]),
        "row_count": int(sql_run["row_count"] or 0),
    }


def extract_result_column_text(result: dict[str, Any], *, column_name: str) -> str:
    for file_result in result.get("files") or []:
        for result_set in file_result.get("result_sets") or []:
            columns = [str(column) for column in result_set.get("columns") or []]
            if column_name not in columns:
                continue
            column_index = columns.index(column_name)
            rows = result_set.get("rows") or []
            if not rows:
                continue
            row = rows[0]
            if not isinstance(row, list) or len(row) <= column_index:
                continue
            return str(row[column_index] or "")
    raise TelegramCommandError(f"Result column not found in SQL run output: {column_name}", exit_code=1)


def resolve_result_output_dir(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (TOOL_ROOT / path).resolve()


def safe_output_file_name(value: str) -> str:
    name = safe_output_path_component(value)
    return name.strip("._") or "db_ops_export.json"


def safe_output_path_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip()).strip("._")


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _dispatch_logger(config_path: str | Path) -> Any:
    """Return (and cache) a logger that records the background CLI dispatch trace to
    ``<log_dir>/telegram_dispatch.log``. Falls back to ``None`` (print-only) if the
    config cannot be loaded, so dispatch never fails just because logging is unavailable."""
    key = str(config_path or "")
    if key in _DISPATCH_LOGGERS:
        return _DISPATCH_LOGGERS[key]
    logger = None
    try:
        cfg = load_config(config_path) if config_path else load_config()
        logger = setup_app_logger(
            cfg,
            app_name=_DISPATCH_LOG_SCOPE,
            log_scope=_DISPATCH_LOG_SCOPE,
            enable_telegram_alerts=False,
            enable_console=False,
        )
    except Exception:  # noqa: BLE001 - logging must never break command dispatch.
        logger = None
    _DISPATCH_LOGGERS[key] = logger
    return logger


def _dispatch_log(logger: Any, message: str, *, level: str = "logging") -> None:
    """Emit one masked dispatch trace line to both the dispatch logger and stdout
    (routed to the telegram runtime log by patch_stdout). Secrets are masked first."""
    safe = mask_sensitive_text(message)
    if logger is not None:
        try:
            log_event(logger, level=level, message=safe)
        except Exception:  # noqa: BLE001
            pass
    print(safe, flush=True)


def _safe_values_text(values: dict[str, Any]) -> str:
    masked = mask_sensitive_value(values)
    return " ".join(f"{key}={masked.get(key)}" for key in sorted(masked))


def execute_cli_background_command(
    *,
    store: DbOpsStore,
    row: Any,
    command: SupportCommand,
    values: dict[str, Any],
    source_id: str,
) -> dict[str, Any]:
    config = dict(command.action_config or {})
    config_path = str(values.get("config_path") or "")
    dlog = _dispatch_logger(config_path)
    restore_id = str(values.get("restore_id") or "")
    point_in_time = str(values.get("point_in_time") or "")
    node_role = _resolve_node_role(config_path)

    _dispatch_log(
        dlog,
        f"dispatch command_received command={command.command_text} command_id={command.command_id} "
        f"source_id={source_id} node_role={node_role} restore_id={restore_id} point_in_time={point_in_time}",
    )
    _dispatch_log(dlog, f"dispatch parsed_args {_safe_values_text(values)}")

    # Key resolution: report presence only, NEVER the value. Restore needs the secret key
    # to decrypt SMB/SQL credentials, so a missing key must fail loudly here rather than
    # let the detached workflow stop silently after "started".
    key_present = bool(os.environ.get(SECRET_KEY_ENV_VAR, "").strip())
    _dispatch_log(dlog, f"dispatch key_resolution env_var={SECRET_KEY_ENV_VAR} present={key_present}")
    if bool(config.get("requires_secret_key")) and not key_present:
        message = (
            f"Cannot start {command.command_text}: the secret key is not available on this worker. "
            f"Start the worker with --key/--key-base64 (or set {SECRET_KEY_ENV_VAR}) so backup "
            "credentials can be decrypted, then retry."
        )
        _dispatch_log(
            dlog,
            f"dispatch key_missing command={command.command_text} reason=secret_key_not_available",
            level="critical",
        )
        queue_command_reply(
            store=store, row=row, command=command, text=message, source_id=source_id, status="failed",
        )
        return {"_queued_reply_count": 1, "status": "FAILED_NO_KEY"}

    argv = build_cli_argv(config, values)
    working_dir = resolve_working_dir(str(config.get("working_dir") or "tools/db_ops"))
    timeout_seconds = int(config.get("timeout_seconds") or 1800)
    _dispatch_log(
        dlog,
        f"dispatch worker_selection node_role={node_role} working_dir={working_dir} timeout_seconds={timeout_seconds}",
    )
    _dispatch_log(dlog, f"dispatch generated_command_line argv={mask_sensitive_value(argv)}")

    # Carry the resolved secret key (and the rest of the environment) into the child
    # explicitly, so decryption never silently depends on implicit inheritance. Secret
    # parameters ride in here too, never in argv (see command_env).
    child_env = command_env(config, values)

    # Queue the "started" reply first so the user always gets it; an immediate crash then
    # appends a failure reply rather than leaving the workflow appearing to stop silently.
    start_text = str(config.get("start_text") or "Command started.")
    queue_command_reply(
        store=store,
        row=row,
        command=command,
        message_text=render_template(start_text, values),
        source_id=source_id,
        status="started",
    )
    queued_reply_count = 1

    stdout_fd, stdout_path = tempfile.mkstemp(suffix=".cli.stdout.txt")
    stderr_fd, stderr_path = tempfile.mkstemp(suffix=".cli.stderr.txt")
    exit_code_path = _exit_code_path(stdout_path)
    try:
        popen_kwargs: dict[str, Any] = {
            "cwd": working_dir,
            "stdout": os.fdopen(stdout_fd, "w", encoding="utf-8", errors="replace"),
            "stderr": os.fdopen(stderr_fd, "w", encoding="utf-8", errors="replace"),
            "text": False,
            "shell": False,
            "env": child_env,
        }
        launch_argv = argv
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
            # A detached process is not a child of the workflow that later polls it, so on
            # POSIX its exit status cannot be read back — waitpid only works for children.
            # Without this the poller has to guess from the output, and a command that says
            # nothing machine-readable (create-db-docker prints a human summary) is reported
            # as FAILED even when it succeeded. So the process writes its own exit code out.
            command_line = " ".join(shlex.quote(part) for part in argv)
            launch_argv = ["/bin/sh", "-c",
                           f"{command_line}; printf %s $? > {shlex.quote(exit_code_path)}"]
        popen = subprocess.Popen(launch_argv, **popen_kwargs)  # noqa: S603
        popen_kwargs["stdout"].close()
        popen_kwargs["stderr"].close()
    except Exception as exc:
        try:
            os.close(stdout_fd)
        except OSError:
            pass
        try:
            os.close(stderr_fd)
        except OSError:
            pass
        Path(stdout_path).unlink(missing_ok=True)
        Path(stderr_path).unlink(missing_ok=True)
        error_summary = safe_error_summary(exc)
        _dispatch_log(
            dlog,
            f"dispatch subprocess_start_failed command={command.command_text} error={error_summary}",
            level="critical",
        )
        failure_text = str(
            config.get("failure_text") or "Command failed.\nExit code: {exit_code}\nError: {error_summary}"
        )
        queue_command_reply(
            store=store,
            row=row,
            command=command,
            message_text=render_template(failure_text, values | {"exit_code": 1, "error_summary": error_summary}),
            source_id=source_id,
            status="failed",
        )
        return {"_queued_reply_count": queued_reply_count + 1, "status": "FAILED_START"}

    _dispatch_log(dlog, f"dispatch subprocess_started pid={popen.pid} command={command.command_text}")

    # Detect an immediate crash (bad argv, import error, early validation failure) so the
    # user gets a real error instead of the workflow appearing to stop after "started".
    grace_seconds = int(config.get("startup_grace_seconds") or 3)
    try:
        early_rc: int | None = popen.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        early_rc = None
    if early_rc is not None and early_rc != 0:
        stderr_text = _read_file_safe(stderr_path)
        stdout_text = _read_file_safe(stdout_path)
        error_detail = _extract_error_from_output(stderr_text, stdout_text)
        _dispatch_log(
            dlog,
            f"dispatch subprocess_exited_early pid={popen.pid} exit_code={early_rc} "
            f"error={_format_dispatch_value(error_detail)}",
            level="critical",
        )
        failure_text = str(
            config.get("failure_text") or "Command failed.\nExit code: {exit_code}\nError: {error_summary}"
        )
        queue_command_reply(
            store=store,
            row=row,
            command=command,
            message_text=render_template(
                failure_text, values | {"exit_code": early_rc, "error_summary": error_detail}
            ),
            source_id=source_id,
            status="failed",
        )
        _remove_file_safe(stdout_path)
        _remove_file_safe(stderr_path)
        return {"_queued_reply_count": queued_reply_count + 1, "pid": popen.pid, "exit_code": early_rc, "status": "FAILED_EARLY"}

    # Still running (normal long restore) or already finished cleanly: hand off to the
    # background-task poller, which reports the final success/failure on a later cycle.
    store.insert_telegram_background_task(
        chat_id=str(row["chat_id"]),
        message_id=int(row["message_id"]) if row["message_id"] is not None else None,
        user_id=str(row["user_id"] or ""),
        command_id=command.command_id,
        command_text=command.command_text,
        source_id=source_id,
        pid=popen.pid,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        task_data={
            "timeout_seconds": timeout_seconds,
            "values": mask_sensitive_value(values),
            "success_text": str(config.get("success_text") or "Command completed."),
            "failure_text": str(
                config.get("failure_text")
                or "Command failed.\nExit code: {exit_code}\nError: {error_summary}"
            ),
            "timeout_text": str(
                config.get("timeout_text")
                or "Command timed out after {timeout_seconds} seconds."
            ),
            "success_output_contains": str(config.get("success_output_contains") or ""),
            "completion_probe": _render_completion_probe(config.get("completion_probe"), values),
        },
    )
    _dispatch_log(
        dlog,
        f"dispatch subprocess_tracking pid={popen.pid} command={command.command_text} "
        f"state={'finished_fast' if early_rc == 0 else 'running'}",
    )
    return {"_queued_reply_count": queued_reply_count, "pid": popen.pid}


def _format_dispatch_value(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def _tail_text(text: str, *, max_chars: int = 500) -> str:
    safe = mask_sensitive_text(str(text or "")).strip()
    return safe[-max_chars:] if len(safe) > max_chars else safe


def _render_completion_probe(probe: Any, values: dict[str, Any]) -> dict[str, Any] | None:
    """Render a command's ``completion_probe`` config with the run's values (so
    ``match_metadata`` placeholders like ``{restore_id}`` become concrete) for storage
    in the background task."""
    if not isinstance(probe, dict):
        return None
    rendered = dict(probe)
    match_metadata = probe.get("match_metadata")
    if isinstance(match_metadata, dict):
        rendered["match_metadata"] = {
            str(k): render_template(str(v), values) for k, v in match_metadata.items()
        }
    return rendered


def _job_run_metadata_matches(metadata_json: Any, match_metadata: dict[str, Any]) -> bool:
    if not match_metadata:
        return True
    try:
        meta = json.loads(str(metadata_json or "{}"))
    except (ValueError, TypeError):
        return False
    if not isinstance(meta, dict):
        return False
    return all(str(meta.get(key, "")) == str(value) for key, value in match_metadata.items())


def _probe_completion(store: Any, probe: Any, *, since_created_at: str) -> tuple[str, str] | None:
    """Authoritative completion of a background command from SQLite ``job_runs``.

    Returns ``("success"|"failure", message)`` when a terminal job-run record (matching
    the probe's success/failure ``job_code`` and ``match_metadata``, created at/after the
    task start) exists, else ``None``. The newest matching record wins, so a re-run is
    reflected. This lets the poller report the real outcome even if the detached process
    lingers past the timeout (e.g. a container-side restore that finishes async)."""
    if not isinstance(probe, dict):
        return None
    success_code = str(probe.get("success_job_code") or "")
    failure_code = str(probe.get("failure_job_code") or "")
    if not (success_code or failure_code):
        return None
    match_metadata = probe.get("match_metadata") or {}
    try:
        rows = store.fetch_terminal_job_runs(
            job_codes=[success_code, failure_code], since_created_at=since_created_at
        )
    except Exception:  # noqa: BLE001 - probe is best-effort; fall back to process/marker.
        return None
    for row in rows:
        if not _job_run_metadata_matches(row["metadata_json"], match_metadata):
            continue
        code = str(row["job_code"] or "")
        message = str(row["error_text"] or row["message"] or "")
        if code == failure_code:
            return "failure", message
        if code == success_code:
            return "success", message
    return None


def check_cli_background_tasks(*, sqlite_path: str | Path) -> dict[str, int]:
    store = DbOpsStore(sqlite_path)
    tasks = store.fetch_running_telegram_background_tasks()
    counts = {"checked": len(tasks), "completed": 0, "timed_out": 0, "queued_reply": 0}

    for task in tasks:
        task_data = json.loads(str(task["task_data"] or "{}"))
        pid = int(task["pid"])
        timeout_seconds = int(task_data.get("timeout_seconds") or 1800)
        values = dict(task_data.get("values") or {})
        created_at_str = str(task["created_at"] or "")

        alive = _is_pid_alive(pid)

        # SQLite is the authoritative completion source for jobs that log a terminal
        # record: report the real outcome even if the detached process lingers past the
        # timeout (e.g. a container-side restore that finishes async, so the workflow
        # process is still alive at the timeout although the restore already succeeded).
        probe_result = _probe_completion(
            store, task_data.get("completion_probe"), since_created_at=created_at_str
        )

        # Check for timeout even if process appears alive (only when SQLite has no verdict yet).
        timed_out = False
        if probe_result is not None:
            if alive:
                try:
                    os.kill(pid, 9 if sys.platform != "win32" else 1)
                except OSError:
                    pass
                alive = False
        elif alive and created_at_str:
            try:
                created_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                age_seconds = (datetime.now(timezone.utc) - created_dt).total_seconds()
                if age_seconds > timeout_seconds:
                    timed_out = True
                    alive = False
                    try:
                        os.kill(pid, 9 if sys.platform != "win32" else 1)
                    except OSError:
                        pass
            except (ValueError, OSError):
                pass

        if probe_result is None and alive:
            continue

        # Windows reads the exit code from the process handle; POSIX cannot (the detached
        # process is not our child), so the dispatch had it write its exit code to a file.
        if sys.platform == "win32":
            exit_code = _get_exit_code_windows(pid)
        else:
            exit_code = _read_exit_code_file(_exit_code_path(str(task["stdout_path"] or "")))

        stdout_text = _read_file_safe(str(task["stdout_path"] or ""))
        stderr_text = _read_file_safe(str(task["stderr_path"] or ""))
        parsed = parse_json_from_output(stdout_text) or {}
        status_str = str(parsed.get("status") or "").upper()
        success_output_contains = str(task_data.get("success_output_contains") or "")

        # Completion source, in order of authority: the SQLite terminal record (probe),
        # then the detached process's exit code / structured output / configured marker.
        if probe_result is not None:
            success = probe_result[0] == "success"
        else:
            success_marker_found = bool(
                success_output_contains and success_output_contains in stdout_text
            )
            success = (
                exit_code == 0
                or status_str in ("SUCCESS", "OK")
                or success_marker_found
            ) and not timed_out

        if timed_out:
            message_text = render_template(
                str(task_data.get("timeout_text") or "Command timed out after {timeout_seconds} seconds."),
                values | {"timeout_seconds": timeout_seconds},
            )
            final_status = "timeout"
        elif success:
            message_text = render_template(
                str(task_data.get("success_text") or "Command completed."),
                values | parsed,
            )
            final_status = "done"
        else:
            error_detail = _extract_error_from_output(stderr_text, stdout_text)
            if probe_result is not None and probe_result[1]:
                error_detail = probe_result[1]  # authoritative error from the SQLite job_run record
            message_text = render_template(
                str(
                    task_data.get("failure_text")
                    or "Command failed.\nExit code: {exit_code}\nError: {error_summary}"
                ),
                values
                | parsed
                | {
                    "exit_code": exit_code if exit_code is not None else 1,
                    "error_summary": error_detail,
                },
            )
            final_status = "failed"

        queue_message({
            "store": store_block_from(store),
            "chat_id": str(task["chat_id"]),
            "text": message_text,
            "reply_message_id": int(task["message_id"]) if task["message_id"] is not None else None,
            "status": final_status,
            "note": f"CLI background task completion for {task['command_text']}",
            "source_type": "telegram_background_tasks",
            "source_id": str(task["task_id"]),
            "metadata": {
                "command_id": int(task["command_id"]),
                "command_text": str(task["command_text"]),
                "status": final_status,
                "pid": pid,
                "values": mask_sensitive_value(values),
            },
        }, fallback_store=store)
        store.complete_telegram_background_task(
            task_id=int(task["task_id"]),
            status=final_status,
            result_json=json.dumps(parsed, ensure_ascii=False) if parsed else None,
        )
        dlog = _dispatch_logger("")
        _dispatch_log(
            dlog,
            f"dispatch subprocess_completed command={task['command_text']} pid={pid} "
            f"final_status={final_status} exit_code={exit_code if exit_code is not None else 'unknown'} "
            f"timed_out={timed_out} "
            f"stdout_tail={_format_dispatch_value(_tail_text(stdout_text))} "
            f"stderr_tail={_format_dispatch_value(_tail_text(stderr_text))}",
            level="logging" if final_status == "done" else "critical",
        )
        _remove_file_safe(_exit_code_path(str(task["stdout_path"] or "")))
        _remove_file_safe(str(task["stdout_path"] or ""))
        _remove_file_safe(str(task["stderr_path"] or ""))
        counts["completed"] += 1
        counts["queued_reply"] += 1
        if timed_out:
            counts["timed_out"] += 1

    return counts


def _get_exit_code_windows(pid: int) -> int | None:
    """Return exit code if the process has exited, None if still running or handle unavailable."""
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return 1  # process gone — treat as non-zero exit (safe default)
    try:
        exit_code = ctypes.c_ulong(0)
        if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None if exit_code.value == STILL_ACTIVE else int(exit_code.value)
        return None
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _exit_code_path(stdout_path: str) -> str:
    """Where a detached POSIX command writes its exit code (see the dispatch)."""
    return f"{stdout_path}.rc"


def _read_exit_code_file(path: str) -> int | None:
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _is_zombie(pid: int) -> bool:
    """A finished process whose parent has not reaped it.

    A detached CLI is started with start_new_session, so when the Telegram workflow that spawned
    it exits, the child is re-parented to PID 1 — inside the container that is the db_ops daemon,
    which does not reap orphans. The process then sits in state Z: it has *exited*, but it still
    has a PID, and ``os.kill(pid, 0)`` succeeds on it.

    Treating that as "still running" is what made a failed `create-db-docker` reply nothing for
    half an hour and then report a timeout instead of the real error.
    """
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            fields = handle.read().rsplit(")", 1)[-1].split()
    except (OSError, IndexError):
        return False
    return bool(fields) and fields[0] == "Z"


def _is_pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _get_exit_code_windows(pid) is None
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        # The signal reached it, but a zombie is not running: it is an exit status nobody
        # collected. Reading /proc tells the difference; os.kill cannot.
        return not _is_zombie(pid)


# `docker compose` writes its progress to stderr — "Network x Creating", "Volume y Created",
# pull percentages — so a compose-driven command's stderr is mostly noise. Reporting the head of
# it as "the error" is what hid the real failure behind a wall of "Volume ... Creating".
_PROGRESS_RE = re.compile(
    r"^(container|volume|network|image|service)\s+\S+\s+"
    r"(creating|created|starting|started|stopping|stopped|removing|removed|recreate|recreated|"
    r"pulling|pulled|waiting|healthy|running|built|building|skipped|interrupted)\b",
    re.IGNORECASE,
)
_PULL_RE = re.compile(r"^\S{12}:\s|(pulling from|downloading|extracting|download complete|"
                      r"pull complete|waiting|verifying checksum|already exists)\b", re.IGNORECASE)
_ERROR_RE = re.compile(r"error|failed|failure|exception|traceback|denied|refused|not found|"
                       r"no such|cannot|unable|conflict", re.IGNORECASE)


def _is_progress_noise(line: str) -> bool:
    return bool(_PROGRESS_RE.match(line) or _PULL_RE.match(line))


def _extract_error_from_output(stderr_text: str, stdout_text: str) -> str:
    """The lines that say what went wrong — not the first lines that happened to be printed.

    Progress chatter is dropped, then the lines that look like an error are preferred, taken
    from the *end* (a failure is reported where it happens, not at the top of the log). Only if
    there are none does it fall back to the tail of whatever was printed.
    """
    for text in (stderr_text, stdout_text):
        lines = [line for line in _meaningful_cli_error_lines(text) if not _is_progress_noise(line)]
        if not lines:
            continue
        error_lines = [line for line in lines if _ERROR_RE.search(line)]
        chosen = (error_lines or lines)[-5:]
        return safe_error_summary(Exception("\n".join(chosen)))
    return "CLI command failed; see runtime logs for details."


def _meaningful_cli_error_lines(text: str) -> list[str]:
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return [line.strip() for line in lines if line.strip() and not _is_normal_cli_log_line(line.strip())]


def _is_normal_cli_log_line(line: str) -> bool:
    if line.startswith("[db_ops."):
        return True
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\|LOGGING\|", line):
        return True
    return "|LOGGING|" in line and not any(marker in line.lower() for marker in ("error", "exception", "traceback", "failed"))


def _read_file_safe(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip() if path else ""
    except OSError:
        return ""


def _remove_file_safe(path: str) -> None:
    try:
        if path:
            Path(path).unlink(missing_ok=True)
    except OSError:
        pass


class CliCommandError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.result = result


def _extract_flag_words(
    *, parameters: list[dict[str, Any]], args: list[str]
) -> tuple[list[str], dict[str, str]]:
    """Pull the standalone keyword arguments out of ``args`` before positions are read.

    A parameter declared ``"source": "flag"`` is a word the operator may add anywhere in the
    message (``/spbot_report_hourly_metrics ACME-192-0-2-248 full``). It has to be removed
    *first*, because a ``consume_rest`` positional such as ``target`` would otherwise swallow it
    and hand "ACME-192-0-2-248 full" to the target resolver as one spec.

    Returns the remaining args and ``{name: value}``, where value is the parameter's
    ``present``/``absent`` text - which ``conditional_args`` then turns into real CLI flags.
    """
    flags = [p for p in parameters if str(p.get("source") or "") == "flag"]
    if not flags:
        return list(args), {}
    remaining = list(args)
    values: dict[str, str] = {}
    for parameter in flags:
        name = str(parameter.get("name") or "").strip()
        if not name:
            continue
        words = {str(w).strip().casefold() for w in (parameter.get("flag_words") or []) if str(w).strip()}
        matched = [item for item in remaining if str(item).strip().casefold() in words]
        remaining = [item for item in remaining if str(item).strip().casefold() not in words]
        values[name] = str(parameter.get("present" if matched else "absent") or "")
    return remaining, values


def cli_action_values(
    *, command: SupportCommand, args: list[str], config_path: str | Path,
    chat_id: str | None = None,
) -> dict[str, Any]:
    config = dict(command.action_config or {})
    values: dict[str, Any] = dict(config.get("defaults") or {})
    values["config_path"] = str(config_path)
    values["command_text"] = command.command_text
    values["python"] = sys.executable
    # The chat that asked. A command whose result is a *deliverable* (an xlsx from a SQL task)
    # has to be able to send it back where it was requested; without this the file goes to the
    # target's configured notify chat and the person who ran it never sees it.
    if chat_id:
        values["chat_id"] = str(chat_id)
    parameters = [dict(item) for item in (config.get("parameters") or config.get("args") or [])]
    args, flag_values = _extract_flag_words(parameters=parameters, args=list(args))
    values.update(flag_values)
    for parameter in parameters:
        parameter = dict(parameter)
        name = str(parameter.get("name") or "").strip()
        if not name:
            continue
        if str(parameter.get("source") or "") == "flag":
            continue  # already resolved above, and it occupies no position
        position = int(parameter.get("position", len(values) + 1))
        value = (
            " ".join(args[position - 1:])
            if bool(parameter.get("consume_rest")) and len(args) >= position
            else args[position - 1] if len(args) >= position
            else ""
        )
        if bool(parameter.get("required", True)) and str(value).strip() == "":
            raise TelegramCommandError(f"Missing required argument: {name}.", exit_code=2)
        if str(value).strip() == "" and not bool(parameter.get("required", True)) and name in values:
            # An optional argument that was not typed keeps its `defaults` entry. Assigning the
            # empty string over it is what broke `/spbot_trace_session` with no argument on
            # 2026-08-12: `"session_id":{session_id}` rendered as `"session_id":,` and the CLI
            # rejected its own payload as malformed JSON. `defaults` only means something if an
            # absent optional value falls back to it.
            #
            # Only when a default exists — an optional parameter with none still resolves to "",
            # which is what `conditional_args` tests with `equals`/`not_equals`.
            continue
        if str(parameter.get("validator") or "") == "target_ip" and str(value).strip():
            value = validate_target_ip(value)
        if str(parameter.get("validator") or "") == "regex" and str(value).strip():
            pattern = str(parameter.get("pattern") or "")
            if not pattern or not re.fullmatch(pattern, str(value), flags=re.IGNORECASE):
                raise TelegramCommandError(
                    str(parameter.get("validation_error") or f"Invalid value for {name}."),
                    exit_code=2,
                )
        values[name] = value
        if str(parameter.get("resolve") or "") == "target" and str(value).strip():
            _inject_resolved_target(values, spec=str(value))
    return values


def _inject_resolved_target(values: dict[str, Any], *, spec: str) -> None:
    """Resolve a unified target spec (server_id or '<db_type> <ip> [port]') and inject the
    canonical ``server_id`` (plus ip/db_type/port) into the CLI value map, so a command can be
    built with ``--server-id {server_id}`` from whichever form the user typed."""
    from db_ops.common import data_sources as target_resolve

    try:
        instance = target_resolve.resolve_target_instance(spec)
    except target_resolve.TargetResolveError as exc:
        raise TelegramCommandError(str(exc), exit_code=2) from exc
    values["server_id"] = str(instance.get("server_id") or "")
    values["target_ip"] = str(instance.get("ip") or "")
    values["db_type"] = target_resolve.normalize_db_type(instance.get("db_type"))
    port = instance.get("port")
    values["port"] = int(port) if str(port or "").strip().isdigit() else ""


def command_env(config: dict[str, Any], values: dict[str, Any]) -> dict[str, str]:
    """The child process environment, extended with any ``env_from_parameters`` values.

    A secret parameter (a database password) must not be rendered into argv: a command line is
    readable by every process on the host (`ps`) and is stored verbatim in the background-task
    row. Declaring ``"env_from_parameters": {"DB_OPS_NEW_DB_PASSWORD": "password_text"}`` hands
    the value to the CLI through the environment instead."""
    env = os.environ.copy()
    for env_name, parameter_name in (config.get("env_from_parameters") or {}).items():
        value = str(values.get(str(parameter_name), "") or "")
        if value:
            env[str(env_name)] = value
    return env


def run_configured_cli_command(*, command: SupportCommand, values: dict[str, Any]) -> dict[str, Any]:
    config = dict(command.action_config or {})
    argv = build_cli_argv(config, values)
    working_dir = resolve_working_dir(str(config.get("working_dir") or "tools/db_ops"))
    timeout_seconds = int(config.get("timeout_seconds") or 1800)
    completed = subprocess.run(  # noqa: S603 - argv is built without shell and comes from trusted command config.
        argv,
        cwd=working_dir,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
        check=False,
        env=command_env(config, values),
    )
    result = parse_cli_result(stdout=completed.stdout, stderr=completed.stderr)
    result.update(
        {
            "exit_code": completed.returncode,
            "argv": mask_sensitive_value(argv),
            "working_dir": str(working_dir),
        }
    )
    if completed.returncode != 0:
        error_summary = result.get("error_summary") or result.get("stderr") or result.get("stdout") or f"CLI failed with exit code {completed.returncode}"
        raise CliCommandError(str(error_summary), exit_code=completed.returncode, result=sanitized_cli_result(result))
    return sanitized_cli_result(result)


def build_cli_argv(config: dict[str, Any], values: dict[str, Any]) -> list[str]:
    if isinstance(config.get("command_argv"), list):
        argv = [render_template(str(part), values) for part in config["command_argv"]]
    else:
        template = str(config.get("command_template") or "").strip()
        if not template:
            raise TelegramCommandError("CLI command_template or command_argv is required.", exit_code=2)
        argv = [render_template(part, values) for part in shlex.split(template)]
    for condition in config.get("conditional_args") or []:
        condition = dict(condition)
        parameter = str(condition.get("parameter") or "")
        actual = str(values.get(parameter) or "")
        equals = condition.get("equals")
        not_equals = condition.get("not_equals")
        matches = True
        if equals is not None:
            matches = actual.casefold() == str(equals).casefold()
        if not_equals is not None:
            matches = matches and actual.casefold() != str(not_equals).casefold()
        if matches:
            argv.extend(render_template(str(part), values) for part in condition.get("argv") or [])
    return argv


def resolve_working_dir(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if value.replace("\\", "/") == "tools/db_ops":
        return TOOL_ROOT
    return REPO_ROOT / path


def parse_cli_result(*, stdout: str, stderr: str) -> dict[str, Any]:
    safe_stdout = mask_sensitive_text(stdout.strip())
    safe_stderr = mask_sensitive_text(stderr.strip())
    parsed = parse_json_from_output(safe_stdout)
    if parsed is None:
        parsed = {}
    parsed.setdefault("stdout", safe_stdout)
    parsed.setdefault("stderr", safe_stderr)
    parsed.setdefault("error_summary", _extract_error_from_output(safe_stderr, safe_stdout))
    return parsed


def parse_json_from_output(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
        return dict(data) if isinstance(data, dict) else {"json": data}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            return dict(data) if isinstance(data, dict) else {"json": data}
        except json.JSONDecodeError:
            return None
    return None


def render_template(template: str, values: dict[str, Any]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def sanitized_cli_result(result: dict[str, Any]) -> dict[str, Any]:
    return mask_sensitive_value(result)


def mask_sensitive_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("***" if sensitive_key(str(key)) else mask_sensitive_value(item)) for key, item in value.items()}
    if isinstance(value, list):
        masked: list[Any] = []
        redact_next = False
        for item in value:
            text = str(item)
            if redact_next:
                masked.append("***")
                redact_next = False
                continue
            masked.append(mask_sensitive_text(text))
            if text.lower() in {"-p", "--password", "--token", "--secret", "--key", "--key-base64", "--key_base64"}:
                redact_next = True
        return masked
    if isinstance(value, str):
        return mask_sensitive_text(value)
    return value


def mask_sensitive_text(text: str) -> str:
    masked = re.sub(r"(?i)(password|token|secret|pwd)\s*[:=]\s*[^,\s]+", r"\1=***", str(text))
    masked = re.sub(r"(?i)(-P\s+)(\"[^\"]+\"|'[^']+'|\S+)", r"\1***", masked)
    masked = re.sub(r"(?i)(--key(?:-base64|_base64)?[=\s]+)(\"[^\"]+\"|'[^']+'|\S+)", r"\1***", masked)
    # Oracle/SQL shapes a DB tool echoes back on failure, which the key=value rules above
    # do not cover: `sys/secret@TNS` in a connect string, and `IDENTIFIED BY "secret"`.
    masked = re.sub(r"(?i)\b([A-Za-z][\w$#]*)/(\"[^\"]+\"|'[^']+'|[^\s@/]+)(@\S+)", r"\1/***\3", masked)
    masked = re.sub(r"(?i)(identified\s+by\s+)(\"[^\"]+\"|'[^']+'|\S+)", r"\1***", masked)
    return masked


# After masking, a leftover credential looks like a label followed by something that is not
# the mask. Prose *about* passwords ("the password contains '&'") has no such pair, so it is
# not a leak — blanket-blocking on the word alone destroyed exactly the messages that explain
# a password rule to the operator.
_UNMASKED_CREDENTIAL_RE = re.compile(r"(?i)\b(password|token|secret|pwd)\b\s*[:=]\s*(?!\*\*\*)\S+")


def safe_error_summary(error: object, *, secrets: Iterable[str] = ()) -> str:
    """One line describing a failure, with credential *values* removed.

    ``secrets`` are values known to be sensitive for this run (a ``secret: true`` parameter).
    They are removed literally, whatever shape they appear in — the only reliable way to
    scrub a value a child process echoed back in a format no pattern anticipated.
    """
    text = " ".join(str(error).replace("\r", " ").replace("\n", " ").split())
    if not text:
        return "workflow failed"
    for secret in secrets:
        value = str(secret or "")
        if len(value) >= 4:          # too short to redact without mangling ordinary words
            text = text.replace(value, "***")
    masked = mask_sensitive_text(text)
    # Belt and braces: if something still reads as a live credential, say nothing rather
    # than risk it.
    if _UNMASKED_CREDENTIAL_RE.search(masked):
        return "workflow failed; sensitive error detail hidden"
    return masked[:300]


def secret_values(values: dict[str, Any] | None) -> list[str]:
    """The run's own secret values, for literal redaction out of an error message.

    Pattern masking only catches shapes it anticipates (``password=x``, ``-P x``,
    ``sys/x@tns``). Removing the actual value catches every shape, including a password a
    tool happened to print in plain prose — but only where the raw value is still known,
    which is the synchronous path. A background task stores its values already masked, so
    there is nothing to match there and pattern masking is the only line of defence.
    """
    return [
        str(value)
        for key, value in (values or {}).items()
        if sensitive_key(str(key)) and str(value or "").strip() not in ("", "-", "***")
    ]


def sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in ("password", "token", "secret", "pwd"))


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


def command_key_from_message(command_prefix: str, command_payload: str) -> str:
    prefix = normalize_command_text(command_prefix)
    payload = normalize_command_text(strip_bot_username(first_command_token(command_payload)))
    if payload:
        return f"{prefix}_{payload}"
    return prefix


def parse_command_message(text: str) -> dict[str, Any]:
    tokens = split_command_tokens(text)
    if not tokens:
        return {"command_key": "", "args": []}
    command_token = strip_bot_username(tokens[0])
    return {
        "command_key": normalize_command_text(command_token),
        "args": tokens[1:],
    }


def split_command_tokens(text: str) -> list[str]:
    try:
        return shlex.split(text.strip())
    except ValueError:
        return text.strip().split()


def normalize_command_text(value: str) -> str:
    text = value.strip().lower()
    text = text.lstrip("/")
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def is_unknown_support_command(command_key: str) -> bool:
    return normalize_command_text(command_key).startswith("spbot_")


def first_command_token(value: str) -> str:
    return value.strip().split(maxsplit=1)[0] if value.strip() else ""


def strip_bot_username(value: str) -> str:
    text = value.strip()
    if "@" in text:
        return text.split("@", 1)[0]
    return text


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
