"""The Telegram support commands: who may run what - the commands this node offers, their levels, and the reply a refusal sends.

Split out of ``telegram/command_processor.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``command_processor`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import Any
from db_ops.lib import data_sources, errors
from db_ops.lib.file_lock import FileLock
from db_ops.lib.json_io import atomic_write_text, indent_of
from db_ops.lib.target_flags import is_record_active
from db_ops.lib import node_role as node_role_rule
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
from db_ops.telegram.commands import can_run_command
from db_ops.lib.paths import DEFAULT_DATA_DIR, REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.telegram.command_base import DEFAULT_COMMANDS_PATH, SupportCommand


#: The level an action that runs free SQL should be configured at. `add_sql_task` registers SQL the
#: scheduler later runs *with commit* on the target's own credential, and `sql_to_xlsx` runs any text
#: a rollback does not undo (KILL, RECONFIGURE, xp_cmdshell, DDL on Oracle/MySQL) - both sat at level
#: 10, below the level-50/100 commands they can emulate (review 0.25.0, F8.1). The shipped catalogue
#: says so; a node file set lower is **used as written** (the file is the truth) and warned about.
ACTION_LEVEL_RECOMMENDED = {"add_sql_task": 100, "sql_to_xlsx": 50}
_WARNED_LEVELS: set[str] = set()


def warn_low_level(command_text: str, action_type: str, configured: int) -> str:
    """The warning for a command set below :data:`ACTION_LEVEL_RECOMMENDED`, or ``""``."""
    recommended = ACTION_LEVEL_RECOMMENDED.get(str(action_type or ""))
    if recommended is None or configured < 0 or configured >= recommended:
        return ""
    return (f"telegram: /{command_text} is level {configured}; it runs free SQL ({action_type}), "
            f"which can do what level-{recommended} commands do - set command_type to {recommended} "
            "in telegram_support_commands.json: python -m db_ops.telegram.cli command-level "
            f"--bot-command {command_text} --level {recommended}")


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
        warning = warn_low_level(str(item["command_text"]), str(item.get("action_type") or ""),
                                 int(item.get("command_type", 0)))
        if warning and warning not in _WARNED_LEVELS:
            # Once per process: the catalogue is read on every pass.
            _WARNED_LEVELS.add(warning)
            print(warning, file=sys.stderr)

    return commands


#: This version's own copy of the commands - what `init` seeds a new node's file from.
SHIPPED_COMMANDS_PATH = Path(__file__).resolve().parent / "catalogue" / "telegram_support_commands.json"


def set_command_level(
    *,
    bot_command: str = "",
    level: int | None = None,
    shipped: bool = False,
    dry_run: bool = False,
    commands_path: Path | None = None,
    shipped_path: Path | None = None,
) -> dict[str, Any]:
    """Edit the commands file under its lock - the console writes it too."""
    path = Path(commands_path) if commands_path else DEFAULT_COMMANDS_PATH
    with FileLock(path):
        return _set_command_level_unlocked(
            bot_command=bot_command, level=level, shipped=shipped, dry_run=dry_run, path=path,
            shipped_path=Path(shipped_path) if shipped_path else SHIPPED_COMMANDS_PATH)


def _set_command_level_unlocked(*, bot_command: str, level: int | None, shipped: bool,
                                dry_run: bool, path: Path, shipped_path: Path) -> dict[str, Any]:
    """Give a bot command the level that decides who may run it, without opening the file.

    The third `*-level` command, and missing until 2026-10-03. A node's own
    ``telegram_support_commands.json`` is used as written - the file is the truth - so a node filled
    from another node's bundle keeps that node's levels: the 0.26 soak node ran ``/spbot_add_sql`` at
    10 where this version ships 100, and ``upgrade-config`` does not touch the file, because a level
    is the operator's value and not a file format (the 0.26 sheet, section 6, C1). The log warned
    once and named a hand-edit; this is the edit as a command.

    ``bot_command`` matches the command's name exactly, with or without the leading ``/`` - no
    substring, a level is a permission. ``level`` is what the user and the chat must be cleared to;
    0 is the public tier and -1 switches the command off (``commands.can_run_command``).

    ``shipped`` takes this version's level instead of a typed one, and only ever **raises**: for
    ``bot_command``, or for every command the file holds below the shipped level when none is named.
    A command set above the shipped level, or one this version does not ship, is left as it is.
    """
    if shipped and level is not None:
        raise errors.InvalidRequest("give level or shipped, not both: shipped means this version's "
                                    "own level, whatever it is.")
    if not shipped and level is None:
        raise errors.InvalidRequest("give level (0 is public, -1 switches the command off), or "
                                    "shipped to take this version's level.")
    if level is not None and int(level) < -1:
        raise errors.InvalidRequest("a command level is 0 or more; -1 switches the command off.")
    wanted = str(bot_command or "").strip().lstrip("/")
    if not shipped and not wanted:
        raise errors.InvalidRequest("name the command: bot_command, e.g. spbot_add_sql.")
    if not path.is_file():
        raise errors.NotConfigured(f"no bot commands at {path}; `init` writes this version's.")

    document = json.loads(path.read_bytes().decode("utf-8-sig"))
    records = [item for item in document.get("telegram_support_commands") or []
               if isinstance(item, dict)]
    known = {str(item.get("command_text") or ""): item for item in records}
    if wanted and wanted not in known:
        raise errors.InvalidRequest(
            f"no bot command is named {wanted!r} in {path.name}. Known: {', '.join(sorted(known))}")

    shipped_levels: dict[str, int] = {}
    if shipped:
        if not shipped_path.is_file():
            raise errors.NotConfigured(f"this install carries no shipped commands at {shipped_path}.")
        shipped_document = json.loads(shipped_path.read_bytes().decode("utf-8-sig"))
        shipped_levels = {str(item.get("command_text") or ""): int(item.get("command_type", 0))
                          for item in shipped_document.get("telegram_support_commands") or []
                          if isinstance(item, dict)}
        if wanted and wanted not in shipped_levels:
            raise errors.InvalidRequest(
                f"this version ships no command named {wanted!r}, so it has no level to take. "
                "Give level instead.")

    changed: list[dict[str, Any]] = []
    for name in ([wanted] if wanted else sorted(known)):
        record = known[name]
        before = int(record.get("command_type", 0))
        if shipped:
            after = shipped_levels.get(name)
            # Only upward, and never a command switched off: a level below the shipped one is
            # what this exists to raise, and anything else is a decision somebody made.
            if after is None or before < 0 or before >= after:
                continue
        else:
            after = int(level)  # type: ignore[arg-type]
            if after == before:
                continue
        record["command_type"] = after
        changed.append({"command_text": name, "before": before, "after": after})

    written = bool(changed) and not dry_run
    if written:
        atomic_write_text(path, json.dumps(document, ensure_ascii=False, indent=indent_of(path)) + "\n")
    return {"changed": changed, "written": written, "dry_run": bool(dry_run), "path": str(path),
            "note": ("" if changed else "nothing to change: "
                     + ("no command is below this version's level." if shipped
                        else f"/{wanted} is already at level {level}."))}


#: Every action a support command can take. One set, read by the dispatcher and held against
#: `telegram_support_command.action_type` in shared_config_objects.json by the suite, so a new action
#: cannot be added to one and not the other.
ACTION_TYPES = frozenset({
    "sql_execute", "cli_execute", "add_sql_task", "sql_to_xlsx", "list_server_id",
    "list_all_command", "list_sql_tasks", "list_metrics", "metric_toggle", "create_table_from_xlsx",
})


def _command_runs_on_node(command_node_role: str, node_role: str) -> bool:
    """Telegram support-command role match, mirroring the app-command daemon. ``all``/``both``/
    ``any`` run on every node; otherwise the command's role must equal the node's role. A
    command with no role defaults to ``worker`` (set at load), so undefined/legacy ``spbot_*``
    commands keep being handled by the worker as before."""
    return node_role_rule.runs_on(command_node_role, node_role, default="worker")


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
        if str(item.get("group_id", "")) == chat_id and is_record_active(item):
            return int(item.get("allow_command", 0))
    return 0


def telegram_user_type(path: Path, *, user_id: str) -> int:
    for item in data_sources.load_telegram_users(path):
        if str(item.get("user_id", "")) == user_id and is_record_active(item):
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
